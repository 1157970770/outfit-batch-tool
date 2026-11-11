# miaoda-shell · 妙搭套壳（只部署前端）

> 与 [`../miaoda/`](../miaoda)（NestJS 全栈）是**两条独立路线**，目录、产物、文档互不覆盖。
> 本工程只做一件事：把共用前端打成静态包，上传到飞书妙搭；后端仍是本机 Python 服务。

## 它是什么

```
浏览器 ──HTTPS──> 妙搭静态站点（本工程 dist/）
                     └──HTTPS + Bearer 令牌──> 本机 Python 服务（8765 生产 / 8766 dev）
```

- 前端源码**不复制**：直接构建 `../miaoda/apps/web`（一份源码，两条路线共用，避免两边改漂）。
- 后端零改动，只在 `server/web.json` 打开跨域白名单 `cors_origins`。
- 页面范围默认 `core`（5 个模块：小批量试跑 / 大批量生产 / 历史任务 / 视频模板库 / 管理），`all` 为 11 视图全量。

## 快速开始

前置：`../miaoda` 已装过依赖（`cd ../miaoda && npm install --ignore-scripts`）。

```powershell
cd miaoda-shell
node scripts/build.mjs https://你的后端入口        # → dist/
node scripts/pack.mjs                             # → release/妙搭套壳-前端-<时间>.zip
```

页面范围 / 后端地址也可以走环境变量：

```powershell
$env:VITE_API_BASE='https://backend.example.com'; $env:VITE_VIEW_SCOPE='all'
node scripts/build.mjs
```

## 目录

| 路径 | 作用 |
| --- | --- |
| `scripts/build.mjs` | 用 `../miaoda` 的 vite 构建 `../miaoda/apps/web`，产物落本工程 `dist/` |
| `scripts/pack.mjs` | 零依赖 store 模式 zip，产物落 `release/` |
| `scripts/cors-smoke.mjs` | 验证本机 Python 服务的跨域 + 令牌链路（13 项） |
| `docs/部署.md` | HTTPS 入口、`cors_origins`、上传妙搭、验收与回滚 |

## 与全栈工程的分界

| | `miaoda-shell/`（本工程） | `../miaoda/`（全栈） |
| --- | --- | --- |
| 后端 | 本机 Python（不动） | NestJS（`apps/server`） |
| 产物 | `dist/`、`release/*.zip` | `apps/web/dist`、`apps/server/dist` |
| 上线 | 传静态包 + 一个 HTTPS 入口 | 妙搭跑 Node 容器 + 持久卷 |
| 适合 | 先把妙搭通道打通 | 目标态，不依赖某台 Windows |

> 改动前端页面会同时影响两条路线——这是有意的（一份源码），但**构建产物与部署互不干扰**。
