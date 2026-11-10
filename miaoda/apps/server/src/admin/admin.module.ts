/**
 * 管理端模块：只装配 `AdminService` 与 `AdminController`。
 *
 * 依赖面（全部 READ-ONLY 复用，不改它们的文件）：
 * - `ConfigModule`   → `web.json`（users 真源）、dataDir、上限、单价
 * - `JobsModule`     → 任务列表 / 记录（用量、明细、归档账本都从它算）
 * - `BatchesModule`  → 批次编号（明细里的「批次号」）
 * - `TemplatesModule`→ 候选集与 manifest 体检数字、重载
 *
 * `AdminModule` 自身不注册全局守卫：`app.module.ts` 里已挂 APP_GUARD 的
 * `SessionGuard`，本模块只负责给每个路由打 `@AdminOnly()` 标记。
 */
import { Module } from '@nestjs/common';

import { BatchesModule } from '../batches/batches.module';
import { ConfigModule } from '../config/config.module';
import { JobsModule } from '../jobs/jobs.module';
import { TemplatesModule } from '../templates/templates.module';
import { AdminController } from './admin.controller';
import { AdminService } from './admin.service';

@Module({
  imports: [ConfigModule, JobsModule, BatchesModule, TemplatesModule],
  controllers: [AdminController],
  providers: [AdminService],
  exports: [AdminService],
})
export class AdminModule {}
