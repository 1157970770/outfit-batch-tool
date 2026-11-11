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

- [x] 生图：`provider=images` 用像素尺寸、`provider=gemini` 用「比例@档位」，两套不混用（`p4-imagegen-smoke` 验了 6 个型号的尺寸/质量档）
- [x] 生图 HTTP 200 但无图视为失败且**不自动重试**（会计费）
- [x] 参考图建任务时拷贝到 `files/<id>/refN.<ext>`，uploads 删了也能复现
- [x] 生图任务 2500ms 轮询，仅在有进行中任务时跑；删除任务同步删文件
- [x] PS 去紫：强度 1–100、LAB 中性带内降饱和、结果 `ps/<upload_id>_s<强度>.png` + `.json`（与 Python 逐位一致，`checksum` 相同）
- [x] 素材检索 iframe：懒挂载、切走不卸载、CSP 放行
- [ ] 生图真实出图（老张 API）：只做了选项/校验/权限的离线与冒烟，**未真的生成一张图**（会产生费用，等用户手动验）

## P6 · 自由视频（2026-11-11 新增）

- [x] 选项与限制下发：`configured/reason/model/provider/cos_enabled/modes/resolutions/ratios/durations/defaults/limits/modes_engine` 与 Python `models.options()` 同字段同顺序
- [x] 参数校验顺序与文案逐字一致（空提示词 / 超 20000 字 / 生成方式 / 视频编辑必须有参考视频 / 分辨率 / 参考生视频不许 adaptive / 时长 / 数量 1–4 / 种子 / 参考图 10、视频 5、音频 5 / 总时长 15s 与 30s 两条上限）
- [x] 状态机：`排队中 / 生成中 / 已完成 / 部分完成 / 生成失败 / 已取消`；分镜 `排队中 / 生成中 / 已完成 / 生成失败 / 已取消`
- [x] 重启恢复：进行中的任务落「生成失败」+「服务重启，进行中的任务已中止；云端可能已出片并计费，请到百炼控制台确认。」
- [x] 参考素材上传走原始字节流（`X-Kind` / `X-File-Name`），100MB / 15MB 上限 413；后缀错误文案一致
- [x] 参考图复用 `/api/uploads`（`upload_ids`），参考视频/音频未启用 COS 时直接报错（不静默丢素材）
- [x] 并发：`web.json.videogen_max_inflight`（默认 2，钳 1..8）；同一任务多分镜顺序跑，占快速通道在飞池
- [x] 取消是协作式的：排队中直接取消、生成中保留 `cancel_requested` 并让当前分镜停下；已结束再取消 409「这个任务已经结束，不能取消。」
- [x] 删除：活动中 409「任务还在跑，先取消再删除。」；删除连 `tasks/<id>.json` 与 `files/<id>/` 一起清
- [x] 下载：成片 `/videos/<index>`、参考素材 `/refs/<kind>/<index>`，默认 inline、`?download=1` 附件（`filename*=UTF-8''`）
- [ ] 参考视频/音频真的送云端（需要 COS）：当前未启用 COS 时按 Python 口径直接失败，启用后要补 `reference_video` / `reference_audio` 的 URL
- [ ] `ffprobe` 路径：默认找 `FFPROBE_PATH` → `D:\ffmpeg\...\ffprobe.exe` → PATH；妙搭容器里必须预装 ffmpeg

实测：`scripts/p6-smoke.mjs` **26/26 通过**（登录 → bootstrap 下发选项 → 6 类参数校验 → 建任务 → mock 出片 → 下载 mp4 / 附件 / 参考图回读 → 已结束取消 409 → 删除 200 → 再查 404）；单测 server 144 项（含 videogen 16）。

## P5 · 前端全量与上线

- [ ] 10 个视图视觉回归通过（与 `app/web/static` 版逐页对照；现为 11 个视图，含自由视频）
- [ ] `active-view` 类名与 id 保持，画布 2500ms 轮询可读
- [ ] 画布复用单 root（`CanvasUI.enter/leave`），不二次 `createRoot`
- [ ] Toast 单例、3200ms、后到覆盖
- [ ] 轮询统一：仅「页面激活 + 文档可见 + 有在飞」时轮询
- [ ] 妙搭环境：构建/启动/端口/健康检查/持久卷跑通；`parity.mjs` 在线上全绿

## 套壳模式（2026-11-11 方向调整：前端进妙搭、后端仍在本机）

- [x] `web.json` 的 `cors_origins` 白名单生效：命中才回 `Access-Control-Allow-Origin` + `Allow-Credentials` + `Vary: Origin`
- [x] `OPTIONS` 预检 204，`Allow-Headers` 含 `Authorization`；非白名单来源 403
- [x] 跨域登录响应额外下发 `token`；同源登录响应形状不变（后端字段不出现）
- [x] `Authorization: Bearer` 可访问全部接口（Cookie 带不上时的退路）
- [x] `?access_token=` 可访问接口（`<img>/<video>/<a download>` 发不了请求头）
- [x] 401 响应也带 CORS 头（否则前端只能看到网络错误）
- [x] `mediaUrl()` 只给自己的接口加令牌，COS 签名链接原样返回
- [x] 套壳包构建：`node scripts/build-shell.mjs <后端地址>` → `dist-miaoda/`，`index.html` 内为 `./assets/...` 相对路径
- [x] 冒烟：`scripts/cors-smoke.mjs` **13/13**
- [ ] 后端 HTTPS 入口（反向代理或隧道）——没有它，HTTPS 页面调 HTTP 接口会被浏览器按混合内容拦掉
- [ ] 在妙搭控制台确认交付形态：静态包上传 / 「构建命令 + 产物目录」
- [ ] 真机联调：妙搭页面登录 → 建任务 → 播放/下载视频（跨域 + Range + 令牌三件事一起验）
- [ ] 素材检索 iframe 与画布 `canvas/` 静态包仍指向局域网地址时的表现（当前已知限制）
