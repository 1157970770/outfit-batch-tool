# 文档索引

> 项目当前主线是**局域网网页版**。先读 [AGENTS.md](../AGENTS.md)（入口地图），
> 再看 [architecture.md](architecture.md)（代码架构）。
> 桌面 exe 版自 2026-11-02 起**暂时废弃**，相关文档统一归到「历史存档」，仅作参考。

## 一、现行（网页版）

| 文档 | 内容 | 时效 |
| --- | --- | --- |
| [architecture.md](architecture.md) | **代码架构**：分层、线程模型、请求/批次生命周期、额度闸门、数据模型、接口表、前端结构、排障 | 2026-11-02 |
| [project-summary-20261102.md](project-summary-20261102.md) | v0.6.0 现状：参数表、能力清单、运维手册、风险 | 2026-11-02 |
| [account-usage-and-quota.md](account-usage-and-quota.md) | 单 Key 共用、创建/轮询双额度、账户用量与金额口径 | 2026-11-02 |
| [web-lan.md](web-lan.md) | 局域网部署、访问地址、常见网络问题 | 2026-10 |
| [ui-rollback.md](ui-rollback.md) | 界面改版的基线、回滚步骤与改版约定 | 2026-11-02 |
| [web-tag-filter-handover.md](web-tag-filter-handover.md) | 标签筛选改造交接（含清单适配细节） | 2026-10-30 |

## 二、引擎 / 协议 / 存储（两条产品线共用）

| 文档 | 内容 |
| --- | --- |
| [wan-3.0-reference-to-video.md](wan-3.0-reference-to-video.md) | 官方 `wan3.0-video` 接口规格：字段、约束、状态机、计费、存储 |
| [adr-002-aliyun-wan3-official-api.md](adr-002-aliyun-wan3-official-api.md) | 从 RunningHub 切到官方 API 的决策、风险与回滚 |
| [adr-001-cos-client-credential.md](adr-001-cos-client-credential.md) | 客户端持有 COS 密钥的已接受风险与收口方式 |
| [cos-setup.md](cos-setup.md) | 建桶、CAM 最小权限、报错对照、费用 |
| [template-tagging.md](template-tagging.md) | 视频打标工具：标签、导出 Excel 清单 |
| [templates-500.md](templates-500.md) | 模板从本地视频到「程序能用的清单」 |
| [remote-config-design.md](remote-config-design.md) | 服务端下发配置的设计（桌面版授权链路） |
| [prompt-library.md](prompt-library.md) | 提示词库（分支功能，未合入） |

## 三、历史存档（桌面 exe 版，暂时废弃）

| 文档 | 说明 |
| --- | --- |
| [project-overview.md](project-overview.md) | 桌面版架构总览（v0.9，2026-10） |
| [usage-aliyun-wan3.md](usage-aliyun-wan3.md) | 桌面/打包/排错使用手册 |
| [handover.md](handover.md) | 项目交接手册（含资产、凭证、Runbook；部分章节已过时） |
| [project-report-20261026.md](project-report-20261026.md) | 当时的项目报告 |
| [deployment-spec.md](deployment-spec.md) / [distribution-signing.md](distribution-signing.md) | 部署与分发签名 |
| [license-integration.md](license-integration.md) / [license-setup.md](license-setup.md) / [license-handoff.md](license-handoff.md) | 授权服务接入、部署与交接 |
| [password-manager.md](password-manager.md) | 授权密码分配管理 |
| [version-isolation.md](version-isolation.md) | 旧 RH 版与新官方版隔离（含 exe/缓存/安装包） |

## 四、图片素材

| 文件 | 用途 |
| --- | --- |
| `ui-preview.png` | 桌面版界面截图（历史） |
| `ui-preview-batch.png` | 桌面版批量页截图（历史） |
| `config-dialog.png` | 桌面版配置弹窗截图（历史） |

## 维护约定

- **新增现行文档**：放在本节「一、现行」，并在 [README.md](../README.md) 文档索引里同步一行；
- **废弃桌面文档**：不删，移到「历史存档」，文件头加一段 `⏸ 已废弃` 提示；
- **命名**：文件名描述内容而不是当时的实现，例如 `account-usage-and-quota.md` 而不是 `per-account-api-key.md`
  （后者容易让人以为现在仍是“每账户一把 Key”）。
