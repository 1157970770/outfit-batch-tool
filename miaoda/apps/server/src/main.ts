import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import type { NestExpressApplication } from '@nestjs/platform-express';
import * as express from 'express';
import type { NextFunction, Request, Response } from 'express';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { AppModule } from './app.module';
import { acquireInstanceLock } from './common/instance-lock';
import { AppConfigService } from './config/app-config.service';
import { createSecurityMiddleware } from './security/security.middleware';

/** 静态站点目录：`npm run build` 产出在 apps/web/dist。 */
function webDist(): string {
  return path.resolve(__dirname, '..', '..', 'web', 'dist');
}

async function bootstrap(): Promise<void> {
  const app = await NestFactory.create<NestExpressApplication>(AppModule, { bodyParser: false });

  const config = app.get(AppConfigService);
  const logger = new Logger('Bootstrap');

  // 数据目录单实例锁：与 Python 版同口径（端口层面挡不住第二个进程，
  // 两个实例会同时写 jobs/batches 并互相抢带 Cookie 的请求）。
  const lock = acquireInstanceLock(config.dataDir);
  if (!lock) {
    logger.error(`已有一个服务在运行（同一数据目录）：${config.dataDir}`);
    logger.error('请先关闭它再启动；若上次是异常退出，删掉 server.lock.d 后重试。');
    await app.close();
    process.exit(2);
  }
  // 进程怎么退（正常关闭 / 崩溃 / Ctrl+C）都要把锁带走；Nest 的 shutdown hook
  // 只处理优雅关闭，`exit` 事件对两者都生效，且不会和它抢信号处理。
  process.on('exit', () => lock.release());

  // 标签页上传视频走原始字节流：必须在 json 解析之前挂，否则 50MB 会被当 JSON 读进内存。
  // 只对这一个路径生效，其余请求仍走下面的 json/urlencoded。
  app.use('/api/tags/videos/upload', express.raw({ type: () => true, limit: '52mb' }));
  // 自由视频的参考视频/音频同理（上限 100MB，留 1MB 余量）。
  app.use('/api/videogen/upload', express.raw({ type: () => true, limit: '101mb' }));
  // 与 Python 一致：只认 Content-Length，不接受 chunked（安全中间件里已拦 411）。
  app.use(express.json({ limit: '32mb' }));
  app.use(express.urlencoded({ extended: false, limit: '2mb' }));
  app.use(createSecurityMiddleware(config));
  app.disable('x-powered-by');
  app.set('trust proxy', false);

  const dist = webDist();
  if (fs.existsSync(path.join(dist, 'index.html'))) {
    app.use(express.static(dist, { index: false, maxAge: 0 }));
    app.use((req: Request, res: Response, next: NextFunction) => {
      if (req.method !== 'GET' && req.method !== 'HEAD') return next();
      const url = req.path || '/';
      if (url.startsWith('/api/') || url === '/healthz' || path.extname(url)) return next();
      res.setHeader('Cache-Control', 'no-store');
      res.sendFile(path.join(dist, 'index.html'));
    });
  } else {
    logger.warn(`未找到前端产物：${dist}（开发时请用 vite 的 5173 端口）`);
  }

  app.enableShutdownHooks();
  await app.listen(config.port, config.host);

  logger.log(`妙搭后端已启动：http://${config.host}:${config.port}`);
  logger.log(`  配置目录 ${config.serverDir}`);
  logger.log(`  数据目录 ${config.dataDir}`);
  logger.log(`  实例锁 ${lock.dir}`);
  if (config.devMode) logger.log('  当前为【开发版】');
}

void bootstrap();
