import { Module } from '@nestjs/common';

import { UploadsModule } from '../uploads/uploads.module';
import { PsController } from './ps.controller';
import { PsService } from './ps.service';

@Module({
  imports: [UploadsModule],
  controllers: [PsController],
  providers: [PsService],
  exports: [PsService],
})
export class PsModule {}
