# 自由视频 tab · 通用 wan3.0 生产（v0.20.0）

> 目标：不选模板、不填款号、不限定换装，直接拿 wan3.0 生产任意视频。
> 与「小批量试跑 / 大批量生产」是**两条互不相干的链路**：那边是「定妆图 + 模板 → 换装」，
> 这边是「提示词 + 任意参考素材 → 视频」，任务不落 `web-data/jobs`，不进 `BatchRunner`，
> 也不参与模板/COS 归档。

## 1. 界面在哪

| | 说明 |
| --- | --- |
| tab | `自由视频`（`index.html`，`data-view="videogen-view"`，排在「生图」后、`画布` 前；所有登录用户可见） |
| 前端 | `app/web/static/videogen.js`（`window.VideoGenUI = { enter, refresh, leave }`）+ `videogen.css` |
| 入口 | 首次点 tab 时由 `app.js::switchView` 调 `VideoGenUI.enter()`；`restoreView` 白名单含 `videogen-view` |
| 左栏 | 提示词 / 反向提示词 / 参考图（≤10）/ 参考视频（≤5）/ 参考音频（≤5）/ 模式·分辨率·比例 / 时长·条数·种子 / 配音·扩写·水印 |
| 右栏 | 生成记录：状态徽标、进度、参考素材缩略、分镜视频网格（可直接播放/下载）、取消与删除 |
| 轮询 | 4 秒一次，仅在本页可见且存在进行中任务时轮询，切走即停 |

三种典型用法：

1. **文生视频**：只写提示词 → 模式留「自动」→ 引擎按 `mode=reference` 提交。
2. **参考图生视频**：拖入 1–10 张图，提示词里写「图1 的人物…」。
3. **视频编辑 / 改风格**：拖入 1–5 段参考视频（可再配音频），提示词写「视频1 …」→ 模式「自动」会推断成 `edit`。

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/videogen/tasks?limit=40` | 任务列表（普通账号只看自己，admin 可看全部）；`{ ok, items[], configured, reason }` |
| GET | `/api/videogen/tasks/<id>` | 单个任务详情 |
| POST | `/api/videogen/tasks` | 建任务，JSON：`prompt, negative_prompt, mode, resolution, ratio, duration, count, seed, audio, prompt_extend, watermark, upload_ids[], video_ids[], audio_ids[]` → 201 |
| POST | `/api/videogen/upload` | **原始字节流**上传参考视频/音频：`X-Kind: video\|audio`、`X-File-Name`（URL 编码）、body 为文件本身 → `{ ok, upload_id, kind, name, size_mb, duration? }` |
| GET | `/api/videogen/tasks/<id>/videos/<n>` | 第 n 条成品（支持 Range；`?download=1` 变附件） |
| GET | `/api/videogen/tasks/<id>/refs/<kind>/<n>` | 参考素材回看（kind ∈ image/video/audio） |
| POST | `/api/videogen/tasks/<id>/cancel` | 取消；排队中直接置「已取消」，生成中打断轮询 |
| DELETE | `/api/videogen/tasks/<id>` | 删除任务（生成中 409） |

参考图不走上表的上传接口，直接复用 `POST /api/uploads`（JSON + base64，格式/20MB 校验在那边）；
建任务时只传 `upload_ids`。任务创建后图片会**复制**进 `web-data/videogen/files/<id>/image<n>.<ext>`，
uploads 目录后续被删也不影响复现。

## 3. 参数口径（`app/videogen/models.py`）

| 字段 | 取值 | 备注 |
| --- | --- | --- |
| `mode` | `auto` / `reference` / `edit` | `auto`：有参考视频→`edit`，否则→`reference`；`edit` 必须至少 1 段参考视频 |
| `resolution` | `480P` / `720P` / `1080P` | 默认 1080P |
| `ratio` | `adaptive` / `21:9` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16` | 默认 16:9；`reference` 模式不允许 `adaptive`（引擎会拒），选了会自动回落 |
| `duration` | 2–30 或 `-1` | `-1` = 自动（仅 `edit` 有意义）；`reference` 下 `-1` 归 5 |
| `count` | 1–4 | 每次提交都重新提交一次引擎（不是一条任务多段） |
| `seed` | 空 / `-1` / 0–2147483647 | 空表示不传 |
| `audio` | bool | 生成配音 |
| `prompt_extend` | bool | 提示词扩写；传 `fileUrl`/`linkUrl` 时引擎要求必开 |
| `watermark` | bool | 加水印 |

硬约束（超了直接 400，文案中文）：提示词 ≤20000、反向提示词 ≤2000、参考图 ≤10（单张 ≤20MB）、
参考视频 ≤5（单段 1–15 秒、总时长 ≤15 秒、单段 ≤100MB、mp4/mov/m4v）、参考音频 ≤5（wav/mp3、≤15MB）、
输入总时长 + 输出时长 ≤30 秒。

**参考视频 / 音频必须有公网 URL**：官方不接受 Base64，所以 COS 关闭时这两类素材会报
「参考视频/音频需要开启 COS…」——纯文生视频和图生视频不受影响（图片走 Base64）。
参考素材上传到 `{COS clean_prefix}/output/free/`，沿用现有 `output/*` 的 CAM 授权，不用改桶策略。

## 4. 数据落在哪

| 路径 | 内容 |
| --- | --- |
| `web-data/videogen/tasks/<id>.json` | 任务记录（32 位 hex id、owner、参数、`refs`、`shots[]`、状态、消息） |
| `web-data/videogen/files/<id>/` | `image<n>.<ext>` / `video<n>.<ext>` / `audio<n>.<ext>` 参考副本 + `shot-<n>.mp4` 成品 |
| `web-data/videogen/uploads/<id>.json` | 参考视频/音频的元数据（`file` 指向上传的真实文件） |

没有 GC：删任务会连 `files/<id>/` 一起删，但 `uploads/` 里的原文件不回收（与 `web-data/uploads` 同债）。

## 5. 并发与计费

- 独立 `queue.Queue` + 守护线程，线程数取 `server/web.json` 的 `videogen_max_inflight`（默认 2，钳 1–8）；
- 提交与轮询复用进程级的**同一套**限流桶（250 RPM / 15 RPS）和**同一个在飞池**（走 `global_small_inflight()`，
  与快速通道共享 12 个名额）——所以它不会绕过全站额度，也不会挤占大批量主池；
- 计费秒与预估金额用 `estimate_cost(usage, resolution, settings.price_map())` 记在每条 `shots[]` 上，
  但只在 admin 的返回里出现（`public(..., admin=True)`）；
- 先记账再下载，下载失败也算「云端已出片」。

## 6. 已知限制

- **重启不续传**：进程重启时还处于「排队中/生成中」的任务会被标成「生成失败」，不会自动重新提交（与生图 tab 一致）。
- 取消是协作式的：`wait_task` 每 0.5 秒检查一次 `cancel_requested`，但已经提交的引擎任务不会撤回（照常计费）。
- 一条任务里 `count>1` 是**串行**跑的，第 2 条要等第 1 条出片。
- 任务 id 是 32 位 hex，与 job/batch 的 12 位 hex 不同，别混用路径。

## 7. 测试

```bash
python tests/test_videogen.py     # 离线、不花钱：10 组用例，桩掉 WanClient / uploader / 下载
python tests/run_all.py           # 全套（含本 tab）
```

覆盖：bootstrap 选项、文生视频、图生视频、参考视频编辑（含音频）、Range/下载/素材回看、
参数校验 400、owner 隔离与 admin 全量、401、取消、删除。
