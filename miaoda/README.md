# miaoda · 换装批量生产器的妙搭版（NestJS 全栈）

> 本工程是**全栈路线**：React 前端 + NestJS 后端，一起部署到飞书妙搭（目标态，11 个视图全量）。
> 「只把前端搬到妙搭、后端仍跑本机 Python」是**另一条独立路线**，代码在 [`../miaoda-shell/`](../miaoda-shell)，
> 与本工程分开存放、各用自己的产物与文档，互不覆盖。

React 19 + Vite 6 + TypeScript 前端；读现有 `server/*.json` 与 `web-data*/*.json`（格式不变、历史数据零迁移）。

> 方案总览：[../docs/妙搭迁移方案.md](../docs/妙搭迁移方案.md) · 全栈部署：[docs/妙搭部署.md](docs/妙搭部署.md) · 一致性清单：[docs/parity-checklist.md](docs/parity-checklist.md)

## 快速开始

```powershell
npm install --ignore-scripts
npm run dev          # 前端 5173 + NestJS 3001
# 只看前端（接口代理到本机 Python 8766）：npm run dev:web
```

全栈产物：

```powershell
npm run build        # shared → server → web
node apps/server/dist/main.js
```

> 需要「前端静态包 + 本机 Python 后端」的套壳形态，请去 `../miaoda-shell/`（`node scripts/build.mjs <后端地址>`）。


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

阶段验收脚本：`scripts/parity.mjs`、`p1-smoke.mjs`、`p2-smoke.mjs`、`p3-smoke.mjs`、`p4-smoke.mjs`、`p4-imagegen-smoke.mjs`、`p5-roles-smoke.mjs`、`p5-cos-smoke.mjs`、`p6-smoke.mjs`（都需要 mock 百炼 + 隔离实例，见各脚本头部注释）。

> `p5-cos-smoke.mjs` 会往真实桶写对象（自动归档）：只在 `cos.prefix` 指向测试前缀时跑，跑完按前缀清理，别在生产 `prod/` 前缀下跑。

## 注意

- **单实例**：任务队列、批次、标签写入都是单进程模型，别对同一 `OUTFIT_DATA_ROOT` 起两个进程。
- **配置不在仓库里**：`server/web.json`（密码哈希 + 会话密钥）与 `server/config.json`（明文 Key）由 `.gitignore` 排除，上线用平台密钥注入。
- **Cookie 名固定** `outfit_sid`，签名与 Python 版同口径——两套系统可以共用同一份 `web.json` 的账号。
