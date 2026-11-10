import { Injectable, Logger } from '@nestjs/common';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { AppConfigService } from '../config/app-config.service';

export interface TagVideo {
  id: string;
  relative_path?: string;
  name?: string;
  duration?: number | null;
  tags?: string[];
  deprecated?: boolean;
  missing?: boolean;
  rating?: number | null;
  [key: string]: unknown;
}

export interface TagStoreFile {
  schema?: number;
  revision?: number;
  updated_at?: string;
  updated_by?: string;
  library?: { version?: number; tags?: string[]; categories?: Record<string, string>; category_order?: string[] };
  videos?: TagVideo[];
  audit?: unknown[];
  [key: string]: unknown;
}

/**
 * 标签真源（`web-data.../template_tags.json`）的只读视图。
 * P0 只提供计数与标签词表；P3 再把写入（revision 乐观锁 + 跨进程锁）补上。
 */
@Injectable()
export class TagCatalogService {
  private readonly logger = new Logger('Tags');
  private cache: { mtimeMs: number; file: string; data: TagStoreFile } | null = null;

  constructor(private readonly config: AppConfigService) {}

  get file(): string {
    return path.join(this.config.dataDir, 'template_tags.json');
  }

  /** mtime 变了才重读：标签页是高频读、低频写。 */
  load(): TagStoreFile {
    const file = this.file;
    let stat: fs.Stats;
    try {
      stat = fs.statSync(file);
    } catch {
      return { revision: 0, videos: [], library: { tags: [], categories: {} } };
    }
    if (this.cache && this.cache.file === file && this.cache.mtimeMs === stat.mtimeMs) return this.cache.data;
    try {
      const data = JSON.parse(fs.readFileSync(file, 'utf8')) as TagStoreFile;
      this.cache = { mtimeMs: stat.mtimeMs, file, data };
      return data;
    } catch (err) {
      this.logger.error(`标签真源不是合法 JSON：${file}（${(err as Error).message}）`);
      return { revision: 0, videos: [], library: { tags: [], categories: {} } };
    }
  }

  get videos(): TagVideo[] {
    const items = this.load().videos;
    return Array.isArray(items) ? items : [];
  }

  get usableVideos(): TagVideo[] {
    return this.videos.filter((item) => !item.deprecated && !item.missing);
  }

  get tagValues(): Record<string, string[]> {
    const tags = this.load().library?.tags;
    const categories = this.load().library?.categories || {};
    const grouped: Record<string, string[]> = {};
    for (const tag of tags || []) {
      const category = String(categories[tag] || '未分类');
      (grouped[category] ||= []).push(tag);
    }
    return grouped;
  }

  get tagCategories(): Record<string, string> {
    return this.load().library?.categories || {};
  }

  get revision(): number {
    return Number(this.load().revision || 0);
  }

  get tagFilterEnabled(): boolean {
    return Object.values(this.tagValues).some((list) => list.length > 0);
  }
}
