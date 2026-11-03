# 文档索引

> 项目只有一条产品线：**局域网网页版**。先读 [AGENTS.md](../AGENTS.md)（入口地图），
> 再看 [architecture.md](architecture.md)（代码架构）。

## 一、网页版

| 文档 | 内容 | 时效 |
| --- | --- | --- |
| [architecture.md](architecture.md) | **代码架构**：分层、线程模型、请求/批次生命周期、额度闸门、数据模型、接口表、前端结构、排障 | 2026-11-02 |
| [project-summary-20261102.md](project-summary-20261102.md) | 现状：参数表、能力清单、运维手册、风险 | 2026-11-02 |
| [account-usage-and-quota.md](account-usage-and-quota.md) | 单 Key 共用、创建/轮询双额度、账户用量与金额口径 | 2026-11-02 |
| [web-lan.md](web-lan.md) | 局域网部署、访问地址、常见网络问题 | 2026-10 |
| [ui-rollback.md](ui-rollback.md) | 界面改版的基线、回滚步骤与改版约定 | 2026-11-02 |
| [web-tag-filter-handover.md](web-tag-filter-handover.md) | 标签筛选改造交接（含清单适配细节） | 2026-10-30 |

## 二、引擎 / 协议 / 存储

| 文档 | 内容 |
| --- | --- |
| [wan-3.0-reference-to-video.md](wan-3.0-reference-to-video.md) | 官方 `wan3.0-video` 接口规格：字段、约束、状态机、计费、存储 |
| [adr-002-aliyun-wan3-official-api.md](adr-002-aliyun-wan3-official-api.md) | 从 RunningHub 切到官方 API 的决策、风险与回滚 |
| [adr-001-cos-client-credential.md](adr-001-cos-client-credential.md) | 客户端持有 COS 密钥的已接受风险与收口方式 |
| [cos-setup.md](cos-setup.md) | 建桶、CAM 最小权限、报错对照、费用 |
| [template-tagging.md](template-tagging.md) | 视频打标工具：标签、导入导出、与网页共用真源 |
| [templates-500.md](templates-500.md) | 模板从本地视频到「程序能用的清单」 |
| [prompt-library.md](prompt-library.md) | 提示词模板库（网页「提示词」下拉的数据来源） |

## 三、评审记录

| 文档 | 内容 | 基线 |
| --- | --- | --- |
| [code-review-20261103.md](code-review-20261103.md) | 全仓代码评审：2 个 P0 / 24 个 P1 / P2 清单，含触发条件、修法、已核实无问题项与修复顺序 | `0abf5f5` |
| [fix-20261103-p0.md](fix-20261103-p0.md) | 第一批修复记录（P0）：调度线程兜底、非成功终态不再报「全部完成」，含行为变化与测试 | `0abf5f5` |

## 维护约定

- **新增文档**：放进对应分组，并在 [README.md](../README.md) 文档索引里同步一行；
- **过期文档**：不删，文件头加一段 `⏸ 已过期` 并注明替代文档；
- **命名**：文件名描述内容而不是当时的实现，例如 `account-usage-and-quota.md` 而不是 `per-account-api-key.md`
  （后者容易让人以为现在仍是“每账户一把 Key”）。
