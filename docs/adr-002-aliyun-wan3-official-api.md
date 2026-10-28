# ADR-002：生成链路切到阿里云百炼官方 Wan 3.0

**状态**：已接受（2026-10-28 真实拉取验证通过：COS 模板 + COS 签名服装图均被官方成功拉取）　**日期**：2026-10-27
**相关**：[wan-3.0-reference-to-video.md](wan-3.0-reference-to-video.md)、[adr-001-cos-client-credential.md](adr-001-cos-client-credential.md)、[cos-setup.md](cos-setup.md)

---

## 1. 背景

现在客户端通过 RunningHub 的包装接口调用万相 3.0：

- `POST /openapi/v2/alibaba/wan-3.0/reference-to-video`
- `POST /openapi/v2/query`
- `POST /openapi/v2/media/upload/binary`

问题：
1. 多一层第三方，单价/稳定性/额度受制于它；
2. 官方 Wan 3.0 是 All-in-One，支持“视频编辑 + 参考图”，更贴近换装场景；
3. 官方 `usage` 更透明（时长/分辨率），便于对账。

目标：保留现有桌面交互、批处理、COS 归档、授权下发，只换生成协议。

---

## 2. 决策

### 2.1 官方优先，RunningHub 保留一版回滚

- 新增 `app/wan_api.py`，把 DashScope 与 RunningHub 两套协议收在一层；
- 默认 `provider="dashscope"`；
- 服务端配置可切 `wan.provider="runninghub"` + `rh_api_key`，客户端不用重打包即可回滚；
- 下一版本再删 RunningHub 代码。

### 2.2 桌面默认 `edit`，CLI 默认 `reference`

- 换装 = 给模板视频换衣服，官方“视频编辑 + 参考图”比纯参考生成更合适；
- `mode="edit"`：media 顺序 `[reference_video, reference_image]`，`ratio=adaptive`，`duration=-1`；
- `mode="reference"`：`[reference_image, reference_video...]`，`ratio=9:16`，`duration=7`；
- 上线前做 2 条真实 A/B（edit vs reference），用 `wan.default_mode` 定默认，不改代码；
- 不自动改写用户 prompt；只在缺编辑意图词时告警。

### 2.3 存储：本分支保留腾讯云 COS

- **输出/归档不改结构**：官方结果 URL 同样 24h，仍走“下载 → 校验 → COS 归档”；
- **输入服装图必改**：RunningHub 上传接口消失；改为上传到
  `{COS_PREFIX}/output/source/<日期>/<指纹>.png`，签 7 天 URL；
  现有子账号只允许 `prod/output/*`，放这里不用改 CAM；
- 无 COS 时图片回退 Base64（官方只对图片支持）；视频/音频没有 Base64，必须 URL；
- 模板视频继续用公有读 COS 直链。

**为什么不在本分支全量迁 OSS**：
- 500 个模板、两个桶、凭证、清单、生命周期都要重做；
- 输出端 24h 逻辑没变，迁了也不减少核心改造；
- `app/storage.py` 已抽象，后续加 `OssStorage` 不影响业务层。

**Day-0 验证结果（2026-10-28）**：官方服务端用 `DashScopeUserBot` 拉素材的担心**已排除**——
实测模板 080（COS 公有 URL）+ 服装图（COS 7 天签名 URL）均被官方成功拉取，任务 `SUCCEEDED`，
成品已归档 COS。**腾讯云 COS 不影响 Wan3 接收参考视频，不需要迁 OSS。**

**Phase 2 仅保留为极端兜底**：如果后续个别模板出现
`GatewayTimeout.InputDownload` / `Failed to download multimodal content` / 403，
先查该 URL 的公网可达性/权限/签名，再考虑单独把该模板放 OSS 或临时改用 Base64（仅图片）；
不做全量 COS → OSS 迁移。

### 2.4 计费口径改为“秒数 + 可选单价”

- 官方 `usage` 没有金额；
- 计费秒数取 `usage.duration`（含输入参考视频，7s + 7s ≈ 14s/条）；
- `wan.price_per_second` 由服务端填，没填就不显示金额；
- `report.csv` 用“计费秒数 / 预估费用”，不再写“扣费”。

### 2.5 提交 at-most-once

- 收到响应后不自动重试，避免重复建任务/重复扣费；
- 只对 429（明确未建任务）和连接建立前失败做有限重试；
- 提交成功立即落盘 `task_id`；
- `UNKNOWN` 标记过期，**不自动重提**，用户显式重跑。

---

## 3. API Key 风险

官方 Key 由服务端随票下发，DPAPI 加密缓存在客户机，和旧 RH Key 同一风险类别，
但百炼 Key 可能覆盖同一业务空间下的更多模型。缓解：

| 措施 | 说明 |
| --- | --- |
| 专用业务空间 | 只放本项目，Key 只在该空间生效 |
| 工作空间级 Key | 不用主账号 Key；不使用全局高权限 Key |
| 消费限额/告警 | 百炼控制台设置预算与告警，超额拒绝优先于扣费 |
| 轮换 | 改 `server/config.json` + deploy，客户端下次启动生效 |
| 停用 | 删除/禁用 Key；`/verify` 每次都回表复查密码状态 |
| 不落代码 | Key 不进仓库、不进 exe、不进日志 |

> `server/config.json` 在 `.gitignore` 里；`config.example.json` 永远为空。
> 现有 `server/config.json` 里的 COS 密钥属于 ADR-001 已接受风险，不在本次范围。

---

## 4. 回滚

1. 服务端 `wan.provider="runninghub"`，保留有效的 `rh_api_key`；
2. 客户端重启即走旧协议；
3. 或 `git revert` 本分支的 `feat(api)` 提交。

RunningHub 代码保留到下一版本；旧客户端只认 `rh_api_key`，所以过渡期服务端同时下发 `wan` 与 `rh_api_key`。

---

## 5. 后果

**正面**：
- 少一层第三方；官方能力（视频编辑/30s/有声）可用；
- `usage` 可对账；错误码更细；
- 服务端可切 provider，灰度/回滚不用发版。

**代价**：
- 服装图要多一次 COS 上传（1 张/批，成本可忽略）；
- 计费口径从“金额”变“秒数”，报表要改；
- 官方限流/审核/地域约束要重新摸；
- 跨云拉取已实测通过（模板 + 签名图），但长期仍要关注官方 `DashScopeUserBot` 的可达性告警。

---

## 6. 重新评估触发条件

| 条件 | 动作 |
| --- | --- |
| 个别模板出现 `media_fetch` 错误 | 先查 URL/权限/签名；该模板单独迁 OSS 或换素材，不做全量迁移 |
| 官方单价明显高于 RunningHub | 重新算成本，考虑 `wan3.0-video-prime` 或回滚 |
| 审核拒绝率 >5% | 评估素材合规/换模型/人工复核 |
| 429 频繁 | 降并发/拉长轮询/申请提额 |
| 需要对外分发 | 重新评估 API Key 下发（服务端代理/STS） |
| 需要统一云厂商 | 做 COS → OSS 全量迁移（另立 ADR） |
