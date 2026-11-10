import { Module, forwardRef } from '@nestjs/common';

import { TagsModule } from '../tags/tags.module';
import { ManifestService } from './manifest.service';
import { PromptsService } from './prompts.service';
import { TemplateCatalogService } from './template-catalog.service';
import { TemplatesController } from './templates.controller';

@Module({
  imports: [forwardRef(() => TagsModule)],
  controllers: [TemplatesController],
  providers: [PromptsService, ManifestService, TemplateCatalogService],
  exports: [PromptsService, ManifestService, TemplateCatalogService],
})
export class TemplatesModule {}
