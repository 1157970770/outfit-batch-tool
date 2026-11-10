import { Module } from '@nestjs/common';

import { ConfigModule } from '../config/config.module';
import { TemplatesModule } from '../templates/templates.module';
import { UploadsModule } from '../uploads/uploads.module';
import { WanModule } from '../wan/wan.module';
import { JobManagerService } from './job-manager.service';
import { JobsController } from './jobs.controller';

@Module({
  imports: [ConfigModule, UploadsModule, TemplatesModule, WanModule],
  controllers: [JobsController],
  providers: [JobManagerService],
  exports: [JobManagerService],
})
export class JobsModule {}
