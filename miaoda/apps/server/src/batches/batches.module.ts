/**
 * 批次 + 历史模块。
 *
 * 依赖：配置（数据目录 / 上限 / 单价 / 账户表）、任务（建 job 与对账读 job）、
 * 模板清单（候选 / 锁定校验 / 模板时长）、上传记录（图 / 校验归属）、提示词库（校验 prompt_id）。
 * 只导出 `BatchManagerService`：其它模块若需要批次数据（例如 `/api/style-usage`）直接注入它。
 */
import { Module } from '@nestjs/common';
import { ConfigModule } from '../config/config.module';
import { JobsModule } from '../jobs/jobs.module';
import { TemplatesModule } from '../templates/templates.module';
import { UploadsModule } from '../uploads/uploads.module';
import { BatchManagerService } from './batch-manager.service';
import { BatchesController } from './batches.controller';
import { HistoryController } from './history.controller';

@Module({
  imports: [ConfigModule, JobsModule, TemplatesModule, UploadsModule],
  controllers: [BatchesController, HistoryController],
  providers: [BatchManagerService],
  exports: [BatchManagerService],
})
export class BatchesModule {}
