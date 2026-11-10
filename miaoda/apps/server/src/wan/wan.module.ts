import { Module } from '@nestjs/common';

import { ConfigModule } from '../config/config.module';
import { WanClientService } from './wan-client.service';

@Module({
  imports: [ConfigModule],
  providers: [WanClientService],
  exports: [WanClientService],
})
export class WanModule {}
