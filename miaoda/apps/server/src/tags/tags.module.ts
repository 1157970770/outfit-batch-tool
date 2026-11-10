import { Module } from '@nestjs/common';

import { TagCatalogService } from './tag-catalog.service';

@Module({
  providers: [TagCatalogService],
  exports: [TagCatalogService],
})
export class TagsModule {}
