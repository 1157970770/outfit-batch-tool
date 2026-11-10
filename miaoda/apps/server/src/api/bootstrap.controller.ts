import { Controller, Get } from '@nestjs/common';

import { CurrentUser, AuthedUser } from '../auth/decorators';
import { AppConfigService } from '../config/app-config.service';
import { PromptsService } from '../templates/prompts.service';
import { TemplateCatalogService } from '../templates/template-catalog.service';

@Controller('api')
export class BootstrapController {
  constructor(
    private readonly config: AppConfigService,
    private readonly catalog: TemplateCatalogService,
    private readonly prompts: PromptsService,
  ) {}

  /**
   * GET /api/bootstrap：字段与 Python 版一致，前端只读这一份就能渲染外壳与上限。
   * 注意 `imagegen` 目前是「未配置」占位，P4 迁移生图模块后替换成真实 options。
   */
  @Get('bootstrap')
  async bootstrap(@CurrentUser() user: AuthedUser) {
    await this.catalog.build();
    const info = this.catalog.info;
    const items = this.catalog.items;
    return {
      ok: true,
      user: { username: user.username, role: user.role },
      service: {
        provider: this.config.provider,
        model: this.config.model,
        dev_mode: this.config.devMode,
        cos_enabled: this.config.cosEnabled,
        templates_count: items.length,
        templates_error: this.catalog.error,
        max_items: this.config.maxJobItems,
        small_lane_items: this.config.smallLaneItems,
        max_batch_items: this.config.maxBatchItems,
        max_batch_run_items: this.config.maxBatchRunItems,
        produce_choices: [5, 10, 20, 50, 100].filter((n) => n <= this.config.maxBatchRunItems),
        produce_min: 5,
        max_images: 10,
        max_concurrency: this.config.maxConcurrency,
        tag_filter_enabled: this.catalog.tagFilterEnabled,
        tagged_templates_count: items.length,
        source_ready_templates: Number(info.source_ready_templates ?? 0),
        untagged_skipped: Number(info.untagged_skipped ?? 0),
        unlisted_files: info.unlisted_files ?? null,
        manifest_warning: String(info.manifest_warning ?? ''),
        notice: this.config.notice,
        imagegen: {
          configured: false,
          reason: '生图模块尚未迁移（P4）',
          models: [],
          default_model: '',
          default_quality: '',
          max_count: 0,
          max_references: 0,
          price: '',
          prompt_limit: 0,
        },
      },
      prompts: this.prompts.list(),
    };
  }
}
