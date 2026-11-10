# 一致性清单（parity checklist）

> 用途：迁移到妙搭版时，逐条对照「Python 版的行为」验收。打勾 = 已实测通过，不是「应该没问题」。
> 自动化的部分跑 `node scripts/parity.mjs <base>`；人工部分在浏览器里点。

## P0 · 骨架与鉴权

- [x] `GET /healthz` → `200 {"ok":true,"version":"0.20.0","dev":<bool>}`
- [x] `GET /api/me` 未登录 → `401 {"ok":false,"error":"登录已过期，请重新登录。"}`
- [x] `POST /api/login` 错误密码 → `401`「账号或密码不正确。」（文案逐字一致）
- [x] `POST /api/login` 正确密码 → `200`，`Set-Cookie: outfit_sid=…; Path=/; HttpOnly; SameSite=Lax; Max-Age=session_hours*3600`
- [x] Python 生成的 PBKDF2 哈希在 Node 侧校验通过（不重置任何账号密码）
- [x] `POST /api/logout` → `200` + `Max-Age=0`
- [x] `GET /api/bootstrap` 字段齐全：`provider/model/dev_mode/cos_enabled/templates_count/max_items/small_lane_items/max_batch_items/max_batch_run_items/produce_choices/produce_min/max_images/max_concurrency/tag_filter_enabled/notice/imagegen`
- [x] `prompts` 下发且已滤掉 `p01 服装定妆`
- [x] 安全头：`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、CSP（`frame-ancestors 'none'`、`frame-src` 放行素材检索）
- [x] 未知 Host → `421`「访问地址无效。」
- [x] 未知 Origin（POST）→ `403`「请求来源无效，请刷新页面后重试。」
- [x] `Transfer-Encoding: chunked` → `411`
- [x] 未知路由 → `404 {"ok":false,"error":"页面或接口不存在。"}`
- [x] SPA 回退：`/`、`/some/deep/link` 都返回 `index.html`；`/assets/*.js` 正常返回
- [x] 前端登录 → 顶栏显示账号与模板数 → 9 个 tab 可切换（管理仅管理员）

## P1 · 小批量试跑

- [x] 图片槽位：1 定妆图必填 + 2–10 细节图可选；**删除后序号左移**；`图N = upload_ids[N-1]`
- [x] 上传：3 并发、250ms 进度刷新；单槽失败不影响其他槽；格式/尺寸校验文案与旧版一致（空图/超限 413，其它 400）
- [x] 场景参数：分辨率/时长/模式/数量；上限与 `max_job_items`、`confirm_over_items` 一致
- [x] 提示词：库列表、6 个款号槽位（上衣必填）、正文 4000 字上限；提交时 `prompt` 为空则回落到库里正文
- [x] 提交：`POST /api/jobs` 返回 job id；`GET /api/jobs/<id>` 轮询 3500ms（`useVisiblePolling`，页面隐藏暂停）
- [x] 终态集合：`succeeded/partial/failed/canceled/interrupted`；`partial` 的失败条目带 `error`
- [x] 取消/重试/复查（`cancel`/`retry`/`recheck`）语义一致；复查返回 202，二次点击 409
- [x] 单条下载与视频 Range 播放（`206` + `Content-Range` + `Accept-Ranges` + `X-Playback: 1` → inline）
- [ ] 额度：提交 250 RPM、轮询 15 RPS 两个独立桶；429 只惩罚对应桶（代码已复刻 `RateLimiter`，**未压测**）
- [ ] 在飞：主池 100、快速通道 12（≤3 条走快速通道；代码已复刻 `Semaphore`，**未压测**）

## P2 · 批次与历史

- [x] 状态机：`draft → locked → closed`；条目 `pending/producing/done/failed`；无独立 `running`（由 `active_job_id` 派生）
- [x] 读时对账：job 状态回写条目（`p2-smoke` 实测 5/5 转 `done`、`active_job_id` 清空）；producing 条目重启后落失败——**未做重启实测**
- [x] 锁定后只能增删细节图（主图不可换），删后序号左移；关闭批次后改图 409
- [x] 生产轮次 5/10/20/50/100；单批 ≤500；单轮 ≤100；重试 1..100
- [x] 一 batch 一文件、原子写；删除批次只删 JSON 不动视频
- [x] 历史分组：`group_by=owner|day|none`；未分组键 `__ungrouped__`（批量分组已实测，`owner`/`day` 仅代码与单测覆盖）
- [ ] 管理员 `?scope=all`、按 `owner` 过滤；普通账号只见自己（**仅单测覆盖，未做双账号端到端**）
- [x] 整批 ZIP：多轮收集、试跑资格判定、`probe` 与 HTTP zip 一致
- [x] 三栏对比层（定妆图 + 生成视频 + 模板视频），Esc/遮罩关闭

## P3 · 视频库与管理端

- [x] 标签真源仍是 `web-data*/template_tags.json`（唯一真源，xlsx 只导入导出；xlsx 相关方法未移植）
- [x] 写入：进程内队列 + 跨进程目录锁（Windows 原子 `mkdir`）+ 锁内重读 + `revision` 比对；冲突 `409` 带 `conflict/revision`，前端自动重拉
- [x] 原子写（tmp + rename）；`.bak` 保留上一版
- [x] 弃用 = 软删除：不删本机视频、不删 COS 对象、可恢复；「标签为空」与「弃用」语义分开
- [x] 版本快照：自动 50 + 手动 50（gzip，格式与 Python 完全兼容，已用真源快照验证）；回滚前另存备份
- [x] 批量打标/评分/上传模态；客户端分页 50/100/200；搜索 300ms 防抖
- [x] 管理端：状态、模板健康、账户用量（含 CSV）、归档检索与签名、用户增删/配额/重置
- [x] **金额只出现在管理端**，试跑/批量/任务操作不显示费用
- [ ] 视频上传的 COS 落盘：当前只登记清单并回 `warning`，不伪造 `key/url`（P4 接存储适配器）
- [ ] Python 文件锁与 Node 目录锁互不识别——同一 `data_dir` 不能混跑两种实现（已知限制，写进部署文档）

实测：`scripts/p3-smoke.mjs` **39/39 通过**（标签写入/revision 冲突/弃用恢复/评分/库操作/版本快照/管理端全部接口/CSV/用户增删）；单测 server 95 项（含 tags 28 + admin 24）。

## P4 · 生图 / PS / 素材

- [ ] 生图：`provider=images` 用像素尺寸、`provider=gemini` 用「比例@档位」，两套不混用
- [ ] 生图 HTTP 200 但无图视为失败且**不自动重试**（会计费）
- [ ] 参考图建任务时拷贝到 `files/<id>/refN.<ext>`，uploads 删了也能复现
- [ ] 生图任务 2500ms 轮询，仅在有进行中任务时跑；删除任务同步删文件
- [ ] PS 去紫：强度 1–100、LAB 中性带内降饱和、结果 `ps/<upload_id>_s<强度>.png` + `.json`；缺 numpy 返回 503
- [ ] 素材检索 iframe：懒挂载、切走不卸载、CSP 放行

## P5 · 前端全量与上线

- [ ] 10 个视图视觉回归通过（与 `app/web/static` 版逐页对照）
- [ ] `active-view` 类名与 id 保持，画布 2500ms 轮询可读
- [ ] 画布复用单 root（`CanvasUI.enter/leave`），不二次 `createRoot`
- [ ] Toast 单例、3200ms、后到覆盖
- [ ] 轮询统一：仅「页面激活 + 文档可见 + 有在飞」时轮询
- [ ] 妙搭环境：构建/启动/端口/健康检查/持久卷跑通；`parity.mjs` 在线上全绿
