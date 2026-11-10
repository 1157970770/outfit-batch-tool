/**
 * 云端模板索引（manifest）。
 *
 * 与 Python `app.core.load_manifest` 同口径：源可以是 http(s) URL 或本地文件，
 * 结构是数组或 `{templates:[...]}`；每条至少要有 `url`（公有桶）或 `key`（私有桶）。
 * 唯一差别：**失败不抛**——记下错误继续跑，界面上给一条 warning（Python 在启动期也会容错）。
 */
import * as fs from 'node:fs';
import { Injectable, Logger } from '@nestjs/common';
import { AppConfigService } from '../config/app-config.service';

export interface ManifestEntry {
  id: string;
  url: string;
  key: string;
  duration?: number | null;
  name?: string;
  shot?: string;
  size_mb?: number | null;
  relative_path?: string;
  filename?: string;
  fingerprint?: string;
}

export interface ManifestResult {
  items: ManifestEntry[];
  error: string;
  fetchedAt: number;
}

const CACHE_MS = 60_000;

@Injectable()
export class ManifestService {
  private readonly logger = new Logger('Manifest');
  private cache: ManifestResult | null = null;

  constructor(private readonly config: AppConfigService) {}

  /** 取 manifest（60 秒缓存）；不可用时 items=[]，error 为原因。 */
  async load(force = false): Promise<ManifestResult> {
    const source = this.config.manifestUrl;
    if (!source) return { items: [], error: '服务端没有配置模板清单', fetchedAt: Date.now() };
    if (!force && this.cache && Date.now() - this.cache.fetchedAt < CACHE_MS) return this.cache;
    try {
      const raw = await this.readSource(source);
      const items = this.parse(raw);
      this.cache = { items, error: '', fetchedAt: Date.now() };
      return this.cache;
    } catch (err) {
      const message = (err as Error).message || String(err);
      this.logger.warn(`模板清单不可用：${message}`);
      this.cache = { items: [], error: message, fetchedAt: Date.now() };
      return this.cache;
    }
  }

  private async readSource(source: string): Promise<string> {
    if (/^https?:\/\//i.test(source)) {
      const res = await fetch(source, { signal: AbortSignal.timeout(30_000) });
      if (!res.ok) throw new Error(`模板清单下载失败：HTTP ${res.status}`);
      return await res.text();
    }
    if (!fs.existsSync(source)) throw new Error(`模板清单不存在：${source}`);
    return fs.readFileSync(source, 'utf8');
  }

  private parse(raw: string): ManifestEntry[] {
    let data: unknown;
    try {
      data = JSON.parse(raw.replace(/^\uFEFF/, ''));
    } catch (err) {
      throw new Error(`模板清单不是合法 JSON：${(err as Error).message}`);
    }
    const list = Array.isArray(data)
      ? data
      : ((data as { templates?: unknown }).templates as unknown[] | undefined);
    if (!Array.isArray(list) || !list.length) throw new Error('模板清单格式不对，应为数组或 {templates:[...]}');

    return list.map((item, index) => {
      const record = typeof item === 'string' ? { url: item } : (item as Record<string, unknown>);
      const id = String(record.id ?? String(index + 1).padStart(3, '0')).padStart(3, '0');
      const url = String(record.url ?? '').trim();
      const key = String(record.key ?? '').trim().replace(/^\/+/, '');
      if (!url && !key) throw new Error(`模板 ${id} 缺少 url（公有桶）或 key（私有桶）`);
      const entry: ManifestEntry = { id, url, key, duration: (record.duration as number) ?? null };
      for (const field of ['name', 'shot', 'size_mb', 'relative_path', 'filename', 'fingerprint'] as const) {
        const value = record[field];
        if (value !== undefined && value !== null && value !== '') {
          (entry as unknown as Record<string, unknown>)[field] = value;
        }
      }
      return entry;
    });
  }
}
