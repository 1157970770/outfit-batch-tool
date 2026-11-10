import { Controller, Get } from '@nestjs/common';

import { Public } from '../auth/decorators';
import { AppConfigService } from '../config/app-config.service';

/** 与 Python `WEB_VERSION` 对齐；前端 PS 页会拿它做版本校验。 */
export const WEB_VERSION = '0.20.0';

@Controller()
export class HealthController {
  constructor(private readonly config: AppConfigService) {}

  @Public()
  @Get('healthz')
  health() {
    return { ok: true, version: WEB_VERSION, dev: this.config.devMode };
  }
}
