import { Controller, Get } from '@nestjs/common';

import { CurrentUser, AuthedUser } from '../auth/decorators';
import { AppConfigService } from '../config/app-config.service';
import { ImagegenManagerService } from '../imagegen/imagegen-manager.service';
import { PromptsService } from '../templates/prompts.service';
import { TemplateCatalogService } from '../templates/template-catalog.service';
import { VideogenManagerService } from '../videogen/videogen-manager.service';

@Controller('api')
export class BootstrapController {
  constructor(
    private readonly config: AppConfigService,
    private readonly catalog: TemplateCatalogService,
    private readonly prompts: PromptsService,
    private readonly imagegen: ImagegenManagerService,
    private readonly videogen: VideogenManagerService,
  ) {}

  /**
   * GET /api/bootstrap：字段与 Python 版一致，前端只读这一份就能渲染外壳与上限。
   * `imagegen` / `videogen` 直接下发各自的 `options()`（与 Python 同字段），
   * 未配置时也是正常返回 configured:false + reason。
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
        imagegen: this.imagegen.options(),
        videogen: this.videogen.options(),
      },
      prompts: this.prompts.list(),
    };
  }
}
