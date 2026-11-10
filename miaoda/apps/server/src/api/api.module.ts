import { Module } from '@nestjs/common';

import { TagsModule } from '../tags/tags.module';
import { TemplatesModule } from '../templates/templates.module';
import { AuthController } from './auth.controller';
import { BootstrapController } from './bootstrap.controller';
import { HealthController } from './health.controller';
import { MeController } from './me.controller';

@Module({
  imports: [TagsModule, TemplatesModule],
  controllers: [AuthController, MeController, BootstrapController, HealthController],
})
export class ApiModule {}
