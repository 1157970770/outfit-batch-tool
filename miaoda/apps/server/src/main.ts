import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import type { NestExpressApplication } from '@nestjs/platform-express';
import * as express from 'express';
import type { NextFunction, Request, Response } from 'express';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { AppModule } from './app.module';
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
  if (config.devMode) logger.log('  当前为【开发版】');
}

void bootstrap();
