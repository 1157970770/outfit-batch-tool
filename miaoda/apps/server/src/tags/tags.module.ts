import { Module, forwardRef } from '@nestjs/common';

import { TemplatesModule } from '../templates/templates.module';
import { TagCatalogService } from './tag-catalog.service';
import { TagsController } from './tags.controller';
import { TagsService } from './tags.service';
import { TemplateStoreService } from './store/template-store.service';

/**
 * 标签模块：只读词表（TagCatalogService）+ 真源单例（TemplateStoreService）+ 标签页业务。
 *
 * `TemplatesModule` 与这里是互相 import（候选集要读词表、标签页要重建候选集），
 * 所以两边都用 `forwardRef` 打环；没有 provider 级环，构造器里直接注入即可。
 */
@Module({
  imports: [forwardRef(() => TemplatesModule)],
  controllers: [TagsController],
  providers: [TagCatalogService, TemplateStoreService, TagsService],
  exports: [TagCatalogService, TemplateStoreService, TagsService],
})
export class TagsModule {}
