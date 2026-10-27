# 100 条 × 7 秒换装视频：生产流程

目标：**1 张定妆图 + 1 套提示词模板 + 100 行服装清单 → 100 条 720P / 7s 竖屏视频**。
本目录的 `tasks.batch.json` 是生成的产物，改清单后重新生成即可，不要手改产物。

## 目录

| 文件 | 说明 |
| --- | --- |
| `outfits.tsv` | 100 行服装清单：`服装 \t 场景 \t 镜头`，Excel / 记事本直接改 |
| `spec.example.json` | 提示词模板 + 规格（720P / 7s / 9:16 / 关智能改写）+ 定妆图路径 |
| `tasks.batch.json` | 生成出来的 100 条任务，交给 `wan3_r2v.py` 跑 |

## 五步走

```bash
# ① 放定妆图（全身、正面、干净背景，长边 ≥1080）
#    路径：assets/定妆图.png

# ② 确认清单（改 outfits.tsv；或重新生成 100 行占位）
python3 make_batch.py --seed-outfits 100 --out batch/outfits.tsv

# ③ 展开成 100 条任务并校验
python3 make_batch.py --spec batch/spec.example.json --out batch/tasks.batch.json

# ④ 先打样 1 条，实测单价与效果（重要，别直接梭哈 100 条）
python3 wan3_r2v.py run --tasks batch/tasks.batch.json --limit 1 --state-file out/batch-state.json

# ⑤ 效果 OK 后跑全量（并发 3 起，边跑边看失败率）
python3 wan3_r2v.py run --tasks batch/tasks.batch.json --concurrency 4 --timeout 1800 --state-file out/batch-state.json
```

断点续跑：`out/batch-state.json` 里已成功的默认跳过；中途超时的任务重跑时**续查**，不会重复扣费。

## 素材怎么给模型：本地路径不行，必须换成 URL

接口只认两种值：**公网可直连 URL** 或 **Base64 Data URI**。本地路径（`D:\...\定妆图.png`）发过去，
服务端读不到你的硬盘，一定会失败。

| 方式 | 适用 | 代价 / 限制 |
| --- | --- | --- |
| 腾讯云 COS 源目录 | 本项目默认 | `COS_*` 环境变量 → 上传 `prod/output/source/` → 签 **7 天** URL；同一文件只传一次 |
| 自有 OSS / CDN 公网直链 | 已有图床 | 必须免登录直连；签名有效期要覆盖生成时长（建议 ≥2 小时） |
| Base64 Data URI | 小的图（≤20 MB，官方只支持图） | **视频/音频不要用**：官方视频只收 URL |
| 官方临时上传 `oss://` | 无 COS 时的兜底 | 48 小时、与模型绑定、100QPS，官方标注“不建议生产” |

**本工具已经自动处理**：`run` 时检测到本地路径 → 有 `COS_*` 就上传换 URL，否则图片转 Base64，视频/音频直接报错。
你的场景是 1 张定妆图供 100 条复用，所以**只上传 1 次，100 条共用同一个 URL**。

```bash
# 想连 URL 一起写进请求文件（真的会上传，需要 Key）
python3 wan3_r2v.py emit --tasks batch/tasks.batch.json --out batch/requests.jsonl --upload
```

自检一个 URL 合不合格：用无痕窗口（或换台机器）打开，**能直接下载文件本体**才合格；
要登录、跳转网页、403、防盗链，都不合格。

## 100 条到底怎么「发」出去

接口是**一条视频一次请求**，没有「批量提交」入口。所以 100 条 = 100 次 POST + N 次 GET + 1 次上传：

```
① 上传定妆图 1 次                         → 拿到 1 个签名 URL（7 天），100 条复用
② POST .../video-synthesis ×100          → 每次立刻返回 task_id（1~2 秒），生成在服务端排队
③ GET  .../tasks/{task_id} ×N（15 秒）   → 到 SUCCEEDED 就立刻下载，不等其他条
```

> 官方查询接口默认 RPS 20；本工具 15 秒轮询 + 抖动 + 10 QPS 限速，批量不会打爆。

并发不是「同时发 100 个」，而是**在途最多 N 个**：并发 4 时，任意时刻只有 4 条在生成，一条完成立刻补一条。
想先看这 100 个请求的原文（不发出、不扣费）：

```bash
python3 wan3_r2v.py emit --tasks batch/tasks.batch.json --out batch/requests.jsonl
```

每行一条：`{"id","endpoint","body"}`，可直接回放或接自建网关。

> 顺带说清一个上限：**单个请求最多 5 段参考视频、总时长 ≤15 秒**。
> 所以「100 个参考视频」不可能一次发给模型，只能拆成 ≥20 个请求，或先剪成 15 秒内的合集。

## 为什么这几个参数是这么定的

| 参数 | 值 | 原因 |
| --- | --- | --- |
| `resolution` | `720P` | 你指定的档位；比 1080P 便宜，7s 短片段清晰度够用 |
| `duration` | `7`（整数） | 官方是整数；`-1` = 智能/保持源时长（edit 模式默认） |
| `aspectRatio` | `9:16` | 官方叫 `ratio`；服装展示/短视频投放；要横屏改成 `16:9` |
| `audio` | `false` | 换装片通常后期配音/配乐，开不开同价，关掉少一层变量 |
| `promptExtend` | `false` | **关键**：开启后 100 条会被大模型各自改写，风格漂移且更慢；关掉 = 100 条严格同构 |
| `seed` | 不传 | 每套服装都需要一点构图自由度；若要卡死机位再加固定 seed |

## 提示词模板的关键句式

```
图1中的同一位女性，保持脸型、五官、发型与妆造完全不变，只更换服装：身穿{outfit}，{scene}，{shot}，真实肤质纹理，柔和电影级布光，画面干净不发灰
```

- **「保持…完全不变 + 只更换服装」** 是换装不换人的核心约束，删了就漂；
- **「图1」** 必须出现，否则模型不知道要用定妆图；
- 场景与镜头也放进模板，100 条才能剪成同一支片子的系列感。

## 想加「模板视频」驱动动作

如果有 7 秒的动作模板视频，把它加进每条任务的 `videoUrls`：

```json
{ "videoUrls": ["assets/template.mp4"], "prompt": "视频1中的动作节奏，图1中的女性……" }
```

约束已内置校验：参考视频单段 1–15s、总时长 ≤15s、且「输入 7s + 输出 7s = 14s ≤ 30s」，合法。
加模板视频会让动作更统一，但也会把画面构图锁死，建议先打样 1 条对比。

## 成本与工期（估算方法，不瞎猜）

1. 先跑 ④ 的 1 条，读 `out/batch-state.json` 里的 `billable_seconds` 与 `usage`；
2. 官方 `usage.duration` **含参考视频输入**：7s 模板 + 7s 输出 ≈ 14s/条，不是 7s；
3. 总价 ≈ `billable_seconds × 单价` × 100；单价在服务端 `wan.price_per_second` 配，未配就只记秒数；
4. 工期 ≈ 100 ÷ 并发数 × 单条耗时；720P/7s 单条约 1–3 分钟，并发 3 大约 50–90 分钟。

并发别一上来就拉满：百度官方查询 RPS 20，提交 RPM 未公开；先 3，观察 429/失败率再调。
`mode="edit"` 会把模板视频当“待编辑视频”，`duration=-1` 保持源时长；`mode="reference"` 才是纯参考生成。
