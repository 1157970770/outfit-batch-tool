import { Module } from '@nestjs/common';

import { ConfigModule } from '../config/config.module';
import { StorageService } from './storage.service';

/** 云端存储：目前只有 COS；ConfigModule 是 @Global 的，这里显式导入只为读起来清楚。 */
@Module({
  imports: [ConfigModule],
  providers: [StorageService],
  exports: [StorageService],
})
export class StorageModule {}
