import { Module } from '@nestjs/common';

import { ConfigModule } from '../config/config.module';
import { UploadsModule } from '../uploads/uploads.module';
import { WanModule } from '../wan/wan.module';
import { VideogenController } from './videogen.controller';
import { VideogenManagerService } from './videogen-manager.service';

/**
 * 自由视频（`/api/videogen/*`）。
 *
 * 依赖：配置、上传（参考图复用 `/api/uploads` 的 upload_id）、Wan 客户端。
 * 与批次/任务互不依赖：不读模板清单、不占 job 槽位。
 */
@Module({
  imports: [ConfigModule, UploadsModule, WanModule],
  controllers: [VideogenController],
  providers: [VideogenManagerService],
  exports: [VideogenManagerService],
})
export class VideogenModule {}
