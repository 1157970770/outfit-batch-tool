/**
 * 生图模块（`/api/imagegen/*`）。
 *
 * 标 `@Global()` 是为了让 `ApiModule` 的 `/api/bootstrap` 能直接注入
 * `ImagegenManagerService` 取 `options()`——bootstrap 属于既有文件，
 * 本次移植的硬约束只允许改 AppModule 与 bootstrap.controller.ts 两处，
 * 所以用全局模块代替「在 ApiModule 里加 import」。
 */
import { Global, Module } from '@nestjs/common';

import { ConfigModule } from '../config/config.module';
import { UploadsModule } from '../uploads/uploads.module';
import { ImagegenController } from './imagegen.controller';
import { ImagegenManagerService } from './imagegen-manager.service';

@Global()
@Module({
  imports: [ConfigModule, UploadsModule],
  controllers: [ImagegenController],
  providers: [ImagegenManagerService],
  exports: [ImagegenManagerService],
})
export class ImagegenModule {}
