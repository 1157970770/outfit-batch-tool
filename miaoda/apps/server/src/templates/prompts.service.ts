import { Injectable, Logger } from '@nestjs/common';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { PromptItem } from '@outfit/shared';
import { AppConfigService } from '../config/app-config.service';

interface PromptFile {
  version?: number;
  prompts?: Array<{ id?: string; title?: string; body?: string }>;
}

/**
 * 提示词库：与 `app/prompts.py` 同一份 `prompts/prompt-library.json`，
 * 并沿用同一条过滤规则——`p01` 与「服装定妆」不进生产页。
 */
@Injectable()
export class PromptsService {
  private readonly logger = new Logger('Prompts');
  private cache: { mtimeMs: number; items: PromptItem[] } | null = null;

  constructor(private readonly config: AppConfigService) {}

  get file(): string {
    return process.env.PROMPT_LIBRARY || path.join(this.config.repoRoot, 'prompts', 'prompt-library.json');
  }

  list(): PromptItem[] {
    const file = this.file;
    let stat: fs.Stats;
    try {
      stat = fs.statSync(file);
    } catch {
      return [];
    }
    if (this.cache && this.cache.mtimeMs === stat.mtimeMs) return this.cache.items;
    try {
      const raw = JSON.parse(fs.readFileSync(file, 'utf8')) as PromptFile;
      const items = (raw.prompts || [])
        .filter((item) => item && String(item.body || '').trim())
        .filter((item) => String(item.id || '') !== 'p01' && String(item.title || '').trim() !== '服装定妆')
        .map((item, index) => ({
          id: String(item.id || `p${index + 1}`),
          title: String(item.title || '').trim(),
          body: String(item.body || '').trim(),
        }));
      this.cache = { mtimeMs: stat.mtimeMs, items };
      return items;
    } catch (err) {
      this.logger.error(`提示词库读取失败：${file}（${(err as Error).message}）`);
      return [];
    }
  }
}
