import { Module } from '@nestjs/common';
import { ConfigModule } from './config/config.module';
import { AuthModule } from './auth/auth.module';
import { TagsModule } from './tags/tags.module';
import { TemplatesModule } from './templates/templates.module';
import { UploadsModule } from './uploads/uploads.module';
import { StorageModule } from './storage/storage.module';
import { WanModule } from './wan/wan.module';
import { JobsModule } from './jobs/jobs.module';
import { BatchesModule } from './batches/batches.module';
import { AdminModule } from './admin/admin.module';
import { PsModule } from './ps/ps.module';
import { ImagegenModule } from './imagegen/imagegen.module';
import { VideogenModule } from './videogen/videogen.module';
import { ApiModule } from './api/api.module';
import { SessionGuard } from './auth/session.guard';
import { ApiExceptionFilter } from './common/api-exception.filter';
import { APP_FILTER, APP_GUARD } from '@nestjs/core';

@Module({
  imports: [
    ConfigModule,
    AuthModule,
    TagsModule,
    TemplatesModule,
    UploadsModule,
    StorageModule,
    WanModule,
    JobsModule,
    BatchesModule,
    AdminModule,
    PsModule,
    ImagegenModule,
    VideogenModule,
    ApiModule,
  ],
  providers: [
    { provide: APP_GUARD, useClass: SessionGuard },
    { provide: APP_FILTER, useClass: ApiExceptionFilter },
  ],
})
export class AppModule {}
