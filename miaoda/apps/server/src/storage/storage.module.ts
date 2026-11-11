import { Global, Module } from '@nestjs/common';

import { ConfigModule } from '../config/config.module';
import { StorageService } from './storage.service';

/** 云端存储：目前只有 COS。设为 @Global 是因为任务/标签/自由视频/模板预览都要用。 */
@Global()
@Module({
  imports: [ConfigModule],
  providers: [StorageService],
  exports: [StorageService],
})
export class StorageModule {}
