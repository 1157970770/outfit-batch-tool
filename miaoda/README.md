# miaoda · 换装批量生产器的妙搭版

React 19 + Vite 6 + TypeScript 前端，NestJS 10 后端，读现有 `server/*.json` 与 `web-data*/*.json`（格式不变、历史数据零迁移）。

> 方案总览：[../docs/妙搭迁移方案.md](../docs/妙搭迁移方案.md) · 部署细节：[docs/妙搭部署.md](docs/妙搭部署.md) · 一致性清单：[docs/parity-checklist.md](docs/parity-checklist.md)

## 快速开始

```powershell
npm install --ignore-scripts
npm run dev          # 后端 3001 + 前端 5173（http://localhost:5173）
```

## 目录

```
miaoda/
├─ packages/shared/      两端共用：状态机常量、上限、DTO
├─ apps/server/          NestJS：配置/鉴权/安全中间件/标签真源/接口
├─ apps/web/             React：外壳 + 11 个视图（含自由视频）
└─ scripts/
   ├─ dev.mjs            先编 shared，再并发起 nest watch 与 vite
   └─ parity.mjs         离线一致性冒烟（状态码/文案/Cookie/安全头/bootstrap）
```

## 脚本

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 开发模式：3001 + 5173 |
| `npm run build` | 全量构建：shared → server → web |
| `npm start` | 跑构建产物（`apps/server/dist/main.js`） |
| `npm run parity` | 一致性冒烟（默认 `http://127.0.0.1:3101`） |
| `npm run typecheck` | 三个包各跑一次 `tsc --noEmit` |

单包操作：`npm run dev --workspace @outfit/web`、`npm run build --workspace @outfit/server`。

## 环境变量

复制 `.env.example` 为 `.env`（不要提交）。最常用的三个：

```text
OUTFIT_SERVER_DIR=../server      # 配置目录
OUTFIT_DATA_ROOT=../web-data-dev # 数据根（相对仓库根，或绝对路径）
PORT=3001
```

## 当前进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 骨架、配置、登录/会话、bootstrap、安全头、静态托管、parity | ✅ |
| P1 | 小批量试跑（图片槽位 insert 交互、提交与轮询、复查） | ✅ |
| P2 | 批次向导、历史分组、ZIP、三栏对比 | ✅ |
| P3 | 视频库打标、管理端 | ✅ |
| P4 | 生图、PS 助手、素材检索 | ✅ |
| P6 | 自由视频（提示词/参考素材/分镜/取消） | ✅ |
| P5 | 视觉回归、画布、妙搭上线 | ⏳ |

阶段验收脚本：`scripts/parity.mjs`、`p1-smoke.mjs`、`p2-smoke.mjs`、`p3-smoke.mjs`、`p4-smoke.mjs`、`p4-imagegen-smoke.mjs`、`p6-smoke.mjs`（都需要 mock 百炼 + 隔离实例，见各脚本头部注释）。

## 注意

- **单实例**：任务队列、批次、标签写入都是单进程模型，别对同一 `OUTFIT_DATA_ROOT` 起两个进程。
- **配置不在仓库里**：`server/web.json`（密码哈希 + 会话密钥）与 `server/config.json`（明文 Key）由 `.gitignore` 排除，上线用平台密钥注入。
- **Cookie 名固定** `outfit_sid`，签名与 Python 版同口径——两套系统可以共用同一份 `web.json` 的账号。
