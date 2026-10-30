# 万相 3.0 换装批量生产线

1 张无头模特图 + 100 个视频模板 → 100 条 7 秒 720P 竖屏换装视频。

> 生成接口已从 RunningHub 包装层切到**阿里云百炼官方** `wan3.0-video`。
> 协议、字段、错误码、存储说明见 [docs/wan-3.0-reference-to-video.md](docs/wan-3.0-reference-to-video.md)；
> 决策与回滚见 [docs/adr-002-aliyun-wan3-official-api.md](docs/adr-002-aliyun-wan3-official-api.md)。
>
> **新分支完整使用手册：[docs/usage-aliyun-wan3.md](docs/usage-aliyun-wan3.md)**（桌面/运维/CLI/排错/回滚）。
> **旧 RH 版与新官方版隔离：[docs/version-isolation.md](docs/version-isolation.md)**（旧版 exe/缓存/zip 不动，可共存）。
> **项目交接手册：[docs/handover.md](docs/handover.md)**（资产与凭证位置、运维 Runbook、现状证据、待办、签字清单）。

## 桌面软件（最终形态，v0.9 → v1.0）

**下载：到 [Releases](../../releases) 里取 `换装批量生产器-v<版本>-<日期>.zip`**（解压后双击文件夹里的 exe）

> 文件夹版：exe 旁边必须有 `_internal\`，分发时发整个 zip，别只发 exe。
> 自己重新打包：双击 `scripts\build.bat`（版本号自动取 `app\main.py` 的 `APP_VERSION`）。

**客户端里没有任何可编辑配置**：服务地址内置在 `app/config.py:18`，百炼 Key / 模板清单 / COS 凭证
全部在输密码激活时由服务端下发，加密缓存到 `%APPDATA%\RHOutfitTool\license.json`（DPAPI）。
原理、运维、排错见 [docs/license-integration.md](docs/license-integration.md)。

![界面](docs/ui-preview.png)

| 页面 | 用途 |
| --- | --- |
| 小批量试跑 | 随机 3 条，验提示词和效果 |
| 大批量生产 | 第 1–5 组（001–500）或随机 100 条，一次跑满 |
| 提示词库 | 六条沉淀提示词，复制或填入生产页 |

> 分组是按**清单顺序每 100 条切一刀**。清单现在只有 108 条，所以**只有第 1 组和「随机 100 条」能跑**，
> 第 2–5 会提示「模板不足 100 个」。补满 500 个模板后 5 组才会全开。

使用说明见 [app/README.md](app/README.md)；源码在 `app/`（`core.py` 引擎 + `wan_api.py` 协议 + `main.py` 界面）。

## 命令行工具（同一套引擎，做批处理/演练用）

| 文件 | 作用 |
| --- | --- |
| [wan3_r2v.py](wan3_r2v.py) | 批量客户端：校验 / 提交 / 轮询 / 下载 / 记账 / emit 官方请求 |
| [make_batch.py](make_batch.py) | 清单 → 100 条任务文件 |
| [tools/build_manifest.py](tools/build_manifest.py) | 本地视频目录 → `manifest.json`（时长/体积校验 + 海报） |
| [tools/upload_templates.py](tools/upload_templates.py) | 视频目录 → 上传 `input/tpl/` + 打标签 + 生成清单 |
| [tools/publish_manifest.py](tools/publish_manifest.py) | 把清单发布到桶根并自检公网可读 |
| [demo.py](demo.py) | 离线演练整条链路，不花钱 |

## 当前状态（2026-10-27）

| 环节 | 状态 | 证据 / 下一步 |
| --- | --- | --- |
| 官方协议适配层 | ✅ 已完成 | `app/wan_api.py`；离线假服务 100 条通过 |
| 离线全链路 | ✅ | `python3 demo.py --count 5` |
| 单元/集成测试 | ✅ | `test_wan_api / test_core / test_storage / test_ui / test_remote_config` |
| 授权服务 `outfit-license` | ✅ 在线 | `GET /health` → `200 {"ok":true,...}` |
| 模板清单 | ✅ 已发布，**108 条**（目标 500） | `https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json` |
| **真实出片** | ✅ **1 条已通** | 2026-10-28：模板 080，480P/5s，`task_id=e8b9cec7...`，成品已归档 COS |
| 官方拉取 COS 素材 | ✅ **已通过** | 模板（COS 公有 URL）+ 服装图（COS 签名 URL）均被官方成功拉取，**不需要迁 OSS** |
| 桌面端实机 + 服务端 deploy | ⬜ 待做 | 需在正常 Windows 桌面双击官方版并跑 3 条；`deploy` 后才有 `wan` 配置 |

> **现在只差**：填 `server/config.json` 的 `wan.api_key` → 打样 1 条 → A/B edit/reference → 补模板到 500。

## 现在就能跑（无需素材、无需 Key、不花钱）

```bash
python3 demo.py --count 5
```

或双击 **`一键演练.bat`**。

它会：造一张占位定妆图 → 起本地假百炼 → 走完 **提交 / 轮询 / 下载 / 记账** → 打印结果表。
产物在 `out/demo/`（假视频，仅证明链路通）。

## 命令行跑真任务（三步）

> 桌面版不需要这一步：Key 由服务端下发。下面只适用于**直接用命令行**跑。

```bash
# 1) 定妆图放到 assets/定妆图.png（全身、正面、干净背景）
# 2) Key 放进环境变量（只影响命令行，不影响桌面版）
set DASHSCOPE_API_KEY=sk-你的Key

# 3) 先打样 1 条，确认换装效果、官方拉取与计费秒数
#    这份清单只有服装图（reference 模式）；桌面版有模板视频，默认走 edit 模式
python3 wan3_r2v.py run --tasks batch/tasks.batch.json --limit 1 --mode reference

# 4) 全量跑（并发 3，轮询 15s，断点续跑）
python3 wan3_r2v.py run --tasks batch/tasks.batch.json --concurrency 3
```

> 官方接口地域/模型/Key 必须一致；`wan.base_url` 可用 `https://dashscope.aliyuncs.com/api/v1`
> 或业务空间专属域名。

## 成片归档到腾讯云 COS

结果链接 **24 小时失效**（官方同样），本地磁盘迟早会满。接一层 COS 后，成片才有长期地址：

- **桌面版**：不用配。COS 桶、前缀与**子账号密钥都由服务端下发**，激活后自动生效；
  每批跑完归档到 `video-output-1463907213/prod/output/<日期>/<批次>/`，归档失败只写进 `archive_error`，不影响出片。
- **服装图**：官方版不再有 RunningHub 上传接口；客户端把图传到
  `video-output-1463907213/prod/output/source/<日期>/`，签 7 天 URL 给模型拉取。
  它在可写前缀内，**不需要改 CAM**；请配 30 天生命周期或批跑完清理。
- **命令行**：手工给凭证（子账号，别用主账号）：

```bash
# 0) 可选：完整 COS 能力/自检脚本；主流程缺包时用标准库 REST 兜底
pip install cos-python-sdk-v5

# 1) 环境变量
set COS_ENABLED=true
set COS_BUCKET=video-output-1463907213
set COS_REGION=ap-guangzhou
set COS_PREFIX=prod
set COS_SECRET_ID=AKIDxxxxxxxx
set COS_SECRET_KEY=xxxxxxxx

# 2) 自检：写 → 签名 → 读 → 列举 → 删，全 ✓ 再继续
python3 scripts/cos_check.py

# 3) 历史成片补传（可重复执行，同一内容不会重复存）
python3 scripts/archive_to_cos.py --root out --dry-run
python3 scripts/archive_to_cos.py --root out
```

> 客户端密钥会随授权下发到客户机，这是**已评估并接受**的风险，边界与收口方式见
> [docs/adr-001-cos-client-credential.md](docs/adr-001-cos-client-credential.md)。
> 百炼 Key 的风险与缓解见 [docs/adr-002-aliyun-wan3-official-api.md](docs/adr-002-aliyun-wan3-official-api.md) §3。

建桶、CAM 最小权限策略、报错对照表、费用估算：见 **[docs/cos-setup.md](docs/cos-setup.md)**。

## 命令速查

| 命令 | 作用 |
| --- | --- |
| `python3 demo.py` | 离线演练整条链路 |
| `python3 wan3_r2v.py validate --tasks batch/tasks.batch.json` | 只校验，不发请求 |
| `python3 wan3_r2v.py build --tasks ... --index 0` | 看第 1 条的真实官方请求体 |
| `python3 wan3_r2v.py emit --tasks ... --out batch/requests.jsonl` | 导出 100 个官方请求原文 |
| `python3 wan3_r2v.py run --tasks ... --limit 1` | 打样 1 条 |
| `python3 wan3_r2v.py run --tasks ... --concurrency 3` | 全量生产 |
| `python3 wan3_r2v.py upload 文件` | 上传素材到 COS，返回 URL |
| `python3 wan3_r2v.py query <task_id>` | 复查某个任务 |
| `python3 scripts/cos_check.py` | COS 连通性自检（不花钱） |
| `python3 scripts/archive_to_cos.py --root out` | 本机成片补传 COS，回写 `cos_key` |

## 文件地图

| 文件 | 说明 |
| --- | --- |
| [docs/handover.md](docs/handover.md) | **项目交接手册：接手第一天做什么、资产/凭证、运维、现状、待办** |
| [batch/README.md](batch/README.md) | **生产流程、参数取舍、素材怎么给、成本工期** |
| [docs/usage-aliyun-wan3.md](docs/usage-aliyun-wan3.md) | **分支使用手册：配置/桌面/CLI/存储/排错/回滚** |
| [docs/version-isolation.md](docs/version-isolation.md) | **旧 RH 版 / 新官方版隔离：分支、exe、缓存、构建产物** |
| [docs/wan-3.0-reference-to-video.md](docs/wan-3.0-reference-to-video.md) | **官方接口规格：字段表、约束、状态机、计费、存储** |
| [docs/adr-002-aliyun-wan3-official-api.md](docs/adr-002-aliyun-wan3-official-api.md) | 切换决策、存储/Key 风险、回滚 |
| [docs/cos-setup.md](docs/cos-setup.md) | **腾讯云 COS 接入手册：建桶 / CAM 策略 / 报错表 / 费用** |
| `app/wan_api.py` | 官方 DashScope / 旧 RunningHub 双协议适配层 |
| `app/media.py` | 图片/视频预检、COS 源图上传、Base64 兜底 |
| `app/storage.py` | 存储适配层（COS / 本地），Key 规则、签名、分片上传 |
| `app/transfer.py` | 远端链接 → 对象存储的流式转存（预留） |
| `scripts/cos_check.py` | 五个原子操作自检 |
| `scripts/archive_to_cos.py` | 历史成片补传 + 回写状态 |
| `batch/outfits.tsv` | 100 行服装清单（服装 / 场景 / 镜头），直接改 |
| `batch/tasks.batch.json` | 生成出来的 100 条任务（改清单后用 make_batch.py 重新生成） |
| `batch/requests.jsonl` | 100 个官方请求原文，逐行可审计 |
| `make_batch.py` | 清单 → 任务文件的展开器 |
| `wan3_r2v.py` | 批量客户端：校验 / 提交 / 轮询 / 下载 / 记账 |
| `tests/mock_dashscope.py` | 本地假百炼（演练用） |

## 生产链路长什么样

```
定妆图（本地）
   └─ 上传 1 次 → COS 签名 URL（7 天）
        └─ 100 条请求共用 imageUrls
             ├─ POST /services/aigc/video-generation/video-synthesis ×100 → task_id（秒回）
             ├─ GET  /tasks/{task_id} 轮询 → SUCCEEDED
             ├─ 立刻下载 → out/*.mp4 + out/batch-state.json
             └─ （可选）同步 COS → output/日期/批次/编号-指纹.mp4
```

## 核心约定（别改错）

- 官方 `duration` 是**整数**：`7` 或 `-1`；旧 RunningHub 才是字符串 `"7"`；
- 素材统一进 `input.media[]`：`reference_image` / `reference_video` / `reference_audio`；
- 换装不换人靠 prompt：`保持脸型、五官、发型与妆造完全不变，只更换服装`，且要有「视频1」「图1」；
- 批量务必 `prompt_extend: false`，否则 100 条风格会漂；
- 结果链接 **24 小时**失效，成功即下载；`task_id` 也只有 24 小时；
- 官方 `usage` 没有金额，按 `usage.duration` 秒数记账，金额以阿里云账单为准。
