# 万相 3.0 · 参考生视频 — API 请求格式（学习笔记 + 可执行规格）

> 接口：`POST https://www.runninghub.cn/openapi/v2/alibaba/wan-3.0/reference-to-video`
> 作用：给一段「参考视频 + 参考图 + 参考音频」，让模型把角色/物件/声音搬进一条新视频里。
> 本文件是本仓库实现 `wan3_r2v.py` 的唯一依据；代码里每条校验都能对应回本文某一节。

---

## 1. 请求链路（4 步）

```
本地素材 ──(可选)上传──> URL/Base64
      │
      ├─1─ POST /reference-to-video  {prompt, videoUrls[], imageUrls[], ...}
      │        └─> {taskId, status:"QUEUED|RUNNING"}
      ├─2─ POST /query  {taskId}  ──轮询──> status:"SUCCESS" + results[0].url
      ├─3─ GET results[0].url  ──24 小时内下载，过期永久失效
      └─4─ （可选）提交时带 webhookUrl，完成时服务端回调，省掉轮询
```

三种异步通知方式，任选其一：轮询 `/query`、提交时加 `webhookUrl`、或两者都做（推荐：webhook 触发 + 轮询兜底）。

---

## 2. 鉴权与请求头

| Header | 值 | 说明 |
| --- | --- | --- |
| `Content-Type` | `application/json` | 固定 |
| `Authorization` | `Bearer <32位API Key>` | 企业级-共享 API Key；标准模型 API 只认这一种 |

- Key 泄露 = 钱包泄露，不要写进前端、不要提交到仓库。
- 本地放在环境变量 `RUNNINGHUB_API_KEY`，由 `wan3_r2v.py` 自动读取。

---

## 3. 提交请求体（核心）

```json
{
  "prompt": "视频1中的角色抱着图1中的物件，坐下来弹奏一支舒缓的乡村民谣，并说道：\"今天的阳光真好。\"",
  "videoUrls": ["https://.../wan-r2v-role2.mp4"],
  "imageUrls": ["https://.../wan-r2v-object-girl.jpg"],
  "audioUrls": [],
  "fileUrl": null,
  "linkUrl": null,
  "resolution": "1080P",
  "aspectRatio": "adaptive",
  "duration": "5",
  "audio": true,
  "seed": null,
  "promptExtend": true,
  "watermark": false
}
```

### 3.1 字段表

| 字段 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `prompt` | String | ✅ | — | 中英文均可；用「视频1 / 图1 / 音频1」按**上传顺序**指代素材 |
| `videoUrls` | String[] | — | — | ≤5 段；单段 1–15 秒；**总时长 ≤15 秒**；单个 ≤100 MB |
| `imageUrls` | String[] | — | — | ≤10 张；每张 ≤20 MB；用于锁角色/道具/场景 |
| `audioUrls` | String[] | — | — | ≤5 段；单段 1–15 秒；总 ≤15 秒；每个 ≤15 MB；**不计入输入计费** |
| `fileUrl` | String | — | null | 文档 URL（docx/doc/xlsx/xls/pptx/ppt/pdf/txt/key/pages/numbers/md）；与 `linkUrl` **互斥**，最多 1 个 |
| `linkUrl` | String | — | null | 公开网页 URL（免登录）；与 `fileUrl` 互斥，最多 1 个；国内不解析海外 URL |
| `resolution` | String | ✅ | `1080P` | `480P` / `720P` / `1080P`；**单价按分辨率区分** |
| `aspectRatio` | String | ✅ | `adaptive` | `adaptive` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16` |
| `duration` | String | ✅ | `5`（注意不是 `auto`） | `auto` 或 `"2"`–`"30"`；**字符串**，不是数字；有参考视频时「输入 + 输出 ≤30 秒」 |
| `audio` | Boolean | — | `true` | 是否有音轨；开/关同价 |
| `seed` | Int | — | null | 0–2147483647 |
| `promptExtend` | Boolean | — | `true` | 智能改写 prompt，短提示提升明显但更慢；**传 fileUrl/linkUrl 时必须 true** |
| `watermark` | Boolean | — | `false` | 是否加水印标识 |

> ⚠️ `duration` 在 JSON 里是**字符串**。写 `"duration": 5` 会被判类型错误——这是最容易踩的坑。

### 3.2 prompt 指代规则（决定成片对不对）

素材按数组**顺序**编号：`videoUrls[0]`→视频1、`imageUrls[0]`→图1、`audioUrls[0]`→音频1。

```jsonc
// 让视频1里的人，用图1的物件，配音频1的声音
"prompt": "视频1中的女孩拿起图1的吉他，按音频1的旋律弹唱，镜头缓慢推近"
```

- 只说「图1」不说动作，模型容易把物件随便放 → prompt 里写清**主体·动作·场景·运镜·台词**。
- 需要口型对台词的，把台词写进 prompt 并用中文引号包裹：`并说道："今天的阳光真好。"`。

### 3.3 硬约束速查（提交前自检）

1. 参考视频总时长 ≤15 秒，单段 1–15 秒；
2. 有参考视频时：`视频总时长 + 输出时长 ≤ 30`；
3. `fileUrl` 与 `linkUrl` 二选一，不能同时出现；
4. 出现 `fileUrl`/`linkUrl` → `promptExtend` 必须是 `true`（且服务端自动开深度思考）；
5. `seed` 范围 0–2147483647；
6. `duration` 是字符串枚举，最大值 30。

> 深入思考模式（fileUrl/linkUrl）会显著增加耗时，批量跑的时候别把它当默认路径。

---

## 4. 提交响应

```json
{
  "taskId": "2013508786110730241",
  "status": "RUNNING",
  "errorCode": "",
  "errorMessage": "",
  "results": null,
  "clientId": "f828b9af25161bc066ef152db7b29ccc",
  "promptTips": "{\"result\": true, ...}"
}
```

- 只拿 `taskId` 去做后续查询；`results` 在提交阶段恒为 `null`。
- 提交成功 ≠ 任务成功，一定要走到 `SUCCESS` 或 `FAILED`。
- `promptTips` 是 ComfyUI 后端的校验信息，排障时看它。

### 4.1 状态机

`QUEUED`（排队）→ `RUNNING`（生成中）→ `SUCCESS` / `FAILED`

到 `FAILED` 时读 `errorCode` / `errorMessage` / `failedReason`；余额不足、素材不合规、审核不过都会落在这里。

---

## 5. 查询结果

```http
POST https://www.runninghub.cn/openapi/v2/query
Authorization: Bearer <KEY>
Content-Type: application/json

{"taskId": "2013508786110730241"}
```

```json
{
  "taskId": "2013508786110730241",
  "status": "SUCCESS",
  "results": [
    { "url": "https://.../output/xxx.mp4", "nodeId": "2", "outputType": "mp4", "text": null }
  ],
  "usage": { "consumeMoney": "0.50", "consumeCoins": "50", "taskCostTime": "96" }
}
```

| 字段 | 用法 |
| --- | --- |
| `results[].url` | **有效期仅 24 小时**，拿到就下载/转存 |
| `results[].outputType` | 扩展名（mp4/png/txt…），用它决定保存后缀 |
| `results[].text` | 纯文本输出才用 |
| `usage.consumeMoney` | 实际扣费（元）；`thirdPartyConsumeMoney` 是三方 API 成本 |
| `usage.taskCostTime` | 运行耗时（秒） |

轮询建议：间隔 5 秒、上限 20–30 分钟；连续 5 次网络失败判为断线而不是任务失败。

---

## 6. Webhook（可选）

提交时在同一个 body 里加：

```json
{ "webhookUrl": "https://your-domain.com/rh/callback" }
```

任务完成时 RunningHub 向该地址 `POST` 任务结果。要点：
- 回调可能重试 → 处理端要**幂等**（按 `taskId` 去重）；
- 回调里同样只有 24 小时有效的 `url`；
- 生产环境建议 webhook 触发 + `/query` 兜底对账。

---

## 7. 素材怎么传（三选一）

| 方式 | 写法 | 适用 |
| --- | --- | --- |
| 公共 URL | `"https://example.com/a.png"` | 素材已有 CDN |
| Base64 Data URI | `"data:image/png;base64,iVBORw0..."` | 小图、免上传；大文件会撑爆请求体 |
| RH 上传接口 | 见下 | 本地文件 / 大文件 |

```http
POST https://www.runninghub.cn/openapi/v2/media/upload/binary
Authorization: Bearer <KEY>
Content-Type: multipart/form-data

file=@/path/to/asset.mp4
```

```json
{ "code": 0, "message": "success",
  "data": { "type": "image", "download_url": "xxxx.png",
            "fileName": "openapi/xxxx.png", "size": "3490" } }
```

- 取 `data.download_url` 塞进 `videoUrls`/`imageUrls`/`audioUrls`；
- **上传得到的链接有效期 1 天**，批量任务要「当天传、当天跑」，别提前一天传好排队。

---

## 8. 计费要点（做预算时按这个算）

1. 输出按 `resolution` 分档单价 × 输出时长；
2. 参考视频按 `min(输入总时长, 15)` 秒计入输入时长；**参考音频不计费**；
3. `audio: true/false` 同价；
4. `duration: "auto"` 按实际成片秒数计费——批量时想控成本就用固定时长；
5. 真实扣费看 `/query` 返回的 `usage.consumeMoney`，别用文档单价硬算。

---

## 9. 批量设计时必须注意的 6 件事

| # | 坑 | 对策（已在本仓库实现） |
| --- | --- | --- |
| 1 | `duration` 传成数字 | 构造器强制转字符串并校验枚举 |
| 2 | 数组字段传成标量 | `videoUrls/imageUrls/audioUrls` 只接受数组 |
| 3 | 参考视频超 15 秒 / 总时长超 30 秒 | 有 ffprobe 就实测，没有则按声明时长校验 |
| 4 | 24 小时后链接失效 | 任务一 `SUCCESS` 立刻下载到本地 |
| 5 | 批量跑一半进程挂了 | `out/state.json` 记录每个任务状态，可断点续跑 |
| 6 | 拼错字段名被静默忽略 | 未知字段直接报错，不提交 |

---

## 10. 本仓库工具用法

```bash
# 0) 先在环境里放 Key（或 --api-key）
set RUNNINGHUB_API_KEY=xxxxxxxx

# 1) 只校验 + 打印将要发送的 JSON，不花钱
python3 wan3_r2v.py build --tasks tasks.example.json --index 0

# 2) 校验全部任务（时长/枚举/互斥/未知字段）
python3 wan3_r2v.py validate --tasks tasks.example.json

# 3) 批量生成（顺序执行；--concurrency 2 可并发提交）
python3 wan3_r2v.py run --tasks tasks.example.json --limit 1

# 4) 单点工具
python3 wan3_r2v.py upload .\assets\role.mp4
python3 wan3_r2v.py query 2013508786110730241
```

产物：`out/<id>.mp4` + `out/state.json`（含 taskId、耗时、实际扣费）。

- `RH_BASE_URL` 可把客户端指向本地假服务/网关，用于不花钱的联调（见 `tests/README.md`）。
- 本地素材路径会自动上传再提交，同一文件只传一次；上传得到的链接 1 天有效，所以「当天传、当天跑」。
- 断点续跑：`out/state.json` 里 `success` 的任务默认跳过，`--force` 重跑，`--fresh` 忽略旧状态。
- 轮询超时不等于失败：记录为 `running` 并保留 `taskId`，重跑时**续查而非重复提交**（避免重复扣费）。
