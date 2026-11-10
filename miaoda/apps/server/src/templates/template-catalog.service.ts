/**
 * 模板候选集：**标签真源（template_tags.json）决定有哪些视频**，manifest 只负责补
 * 云端地址（url/key），按「文件名 / 相对路径 / 指纹 + 大小一致」匹配，**绝不按模板 ID 匹配**
 * ——两套编号体系互不相干，按 ID 配会拿错视频。
 *
 * 口径对齐 `app/web/templates.py::attach_tag_store` 与 `::public/choose`。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { Injectable, Logger } from '@nestjs/common';
import { AppConfigService } from '../config/app-config.service';
import { TagCatalogService } from '../tags/tag-catalog.service';
import { ManifestService, type ManifestEntry } from './manifest.service';

export interface TemplateRow {
  id: string;
  name: string;
  filename: string;
  shot: string;
  relative_path: string;
  tags: string[];
  tag_categories: Record<string, string>;
  upload_state: string;
  duration: number | null;
  size_mb: number | null;
  deprecated: boolean;
  deprecated_at?: unknown;
  deprecated_by: string;
  deprecated_reason: string;
  missing: boolean;
  changed: boolean;
  touched_at?: unknown;
  touched_by: string;
  uploaded_at: number;
  rating: number;
  url: string;
  key: string;
}

export interface PublicTemplate {
  id: string;
  name: string;
  shot: string;
  duration: number | null;
  size_mb: number | null;
  tags: string[];
  tag_categories: Record<string, string>;
  ready: boolean;
  upload_state: string;
}

/** 标签页行：字段与 Python `TemplateCatalog.public_tags()` 一一对应。 */
export interface TagPublicRow {
  id: string;
  name: string;
  filename: string;
  relative_path: string;
  tags: string[];
  tag_categories: Record<string, string>;
  deprecated: boolean;
  deprecated_at: unknown;
  deprecated_by: string;
  ready: boolean;
  upload_state: string;
  duration: number | null;
  size_mb: number | null;
  missing: boolean;
  changed: boolean;
  touched_at: unknown;
  touched_by: string;
  uploaded_at: number;
  rating: number;
}

export class TemplateError extends Error {}

interface CatalogState {
  key: string;
  items: TemplateRow[];
  retired: Map<string, TemplateRow>;
  untagged: Map<string, TemplateRow>;
  error: string;
  info: Record<string, unknown>;
}

const VIDEO_SUFFIXES = ['.mp4', '.mov', '.m4v', '.avi', '.mkv'];

function normalizeId(value: unknown): string {
  const text = String(value ?? '').trim();
  return /^\d+$/.test(text) ? text.padStart(3, '0') : text;
}

function basename(value: unknown): string {
  let text = String(value ?? '').trim();
  if (!text) return '';
  if (text.includes('://')) {
    try {
      text = new URL(text).pathname;
    } catch {
      /* 保持原样 */
    }
  }
  return (text.replace(/\\/g, '/').split('/').pop() ?? '').trim().toLowerCase();
}

function asFloat(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(String(value).trim());
  return Number.isFinite(n) ? n : null;
}

function sizesAgree(a: number | null, b: number | null): boolean {
  if (a === null || b === null) return false;
  return Math.abs(a - b) <= Math.max(0.05, a * 0.01);
}

function uploadState(video: Record<string, unknown>, tags: string[]): string {
  if (video.missing) return '文件不存在';
  if (video.changed) return '文件已变化';
  if (video.deprecated) return '已弃用';
  if (video.uploaded) return '已上传';
  if (!tags.length) return '废弃';
  return '待上传';
}

@Injectable()
export class TemplateCatalogService {
  private readonly logger = new Logger('Templates');
  private cache: CatalogState | null = null;

  constructor(
    private readonly config: AppConfigService,
    private readonly tags: TagCatalogService,
    private readonly manifest: ManifestService,
  ) {}

  /** 组装候选集（按 manifest 抓取时间 + 标签 revision 缓存）。 */
  async build(force = false): Promise<void> {
    const manifest = await this.manifest.load(force);
    const store = this.tags.load();
    const key = `${manifest.fetchedAt}|${this.tags.revision}|${String(store.updated_at ?? '')}`;
    if (!force && this.cache && this.cache.key === key) return;

    const overlay = this.readOverlay();
    const sources = this.mergeSources(manifest.items, overlay);
    const byName = new Map<string, ManifestEntry[]>();
    const byFingerprint = new Map<string, ManifestEntry[]>();
    for (const item of sources) {
      if (!item.url && !item.key) continue;
      for (const name of [basename(item.relative_path), basename(item.filename), basename(item.key), basename(item.url)]) {
        if (!name) continue;
        const bucket = byName.get(name) ?? [];
        if (bucket.length < 8 && !bucket.includes(item)) bucket.push(item);
        byName.set(name, bucket);
      }
      const fingerprint = String(item.fingerprint ?? '').trim().toLowerCase();
      if (fingerprint) {
        const bucket = byFingerprint.get(fingerprint) ?? [];
        bucket.push(item);
        byFingerprint.set(fingerprint, bucket);
      }
    }

    const categories = this.tags.tagCategories;
    const items: TemplateRow[] = [];
    const retired = new Map<string, TemplateRow>();
    const untagged = new Map<string, TemplateRow>();
    let untaggedCount = 0;
    let deprecatedCount = 0;

    const rows = Array.isArray(store.videos) ? store.videos : [];
    for (const video of rows) {
      const record = video as Record<string, unknown>;
      const id = normalizeId(record.id);
      const tags = (Array.isArray(record.tags) ? (record.tags as string[]) : []).filter((tag) => tag in categories);
      const size = asFloat(record.size_mb);
      const row: TemplateRow = {
        id,
        name: String(record.name || `场景 ${id}`),
        filename: String(record.name || ''),
        shot: '',
        relative_path: String(record.relative_path || '').replace(/\\/g, '/'),
        tags,
        tag_categories: Object.fromEntries(tags.map((tag) => [tag, categories[tag]])),
        upload_state: uploadState(record, tags),
        duration: asFloat(record.duration),
        size_mb: size,
        deprecated: Boolean(record.deprecated),
        deprecated_at: record.deprecated_at,
        deprecated_by: String(record.deprecated_by || ''),
        deprecated_reason: String(record.deprecated_reason || ''),
        missing: Boolean(record.missing),
        changed: Boolean(record.changed),
        touched_at: record.touched_at,
        touched_by: String(record.touched_by || ''),
        uploaded_at: Number(record.uploaded_at || 0),
        rating: Number(record.rating || 0),
        url: '',
        key: '',
      };
      const names = [basename(record.relative_path), basename(record.name)].filter(Boolean);
      const fingerprint = String(record.fingerprint ?? '').trim().toLowerCase();
      let candidates: ManifestEntry[] = [];
      if (fingerprint && byFingerprint.has(fingerprint)) candidates = [...byFingerprint.get(fingerprint)!];
      else for (const name of names) candidates.push(...(byName.get(name) ?? []));
      const unique = new Map<ManifestEntry, true>();
      for (const item of candidates) if (sizesAgree(size, asFloat(item.size_mb))) unique.set(item, true);
      const source = unique.size === 1 ? [...unique.keys()][0] : null;
      row.url = String(source?.url ?? '');
      row.key = String(source?.key ?? '');

      if (row.deprecated) {
        retired.set(id, row);
        deprecatedCount += 1;
        continue;
      }
      if (!tags.length) {
        untagged.set(id, row);
        untaggedCount += 1;
        continue;
      }
      items.push(row);
    }

    const byId = new Map<string, TemplateRow>();
    for (const item of items) {
      if (byId.has(item.id)) {
        this.cache = {
          key,
          items: [],
          retired: new Map(),
          untagged: new Map(),
          error: '视频场景清单中存在重复模板ID，无法安全匹配标签表格',
          info: {},
        };
        return;
      }
      byId.set(item.id, item);
    }

    const ready = items.filter((item) => item.url || item.key).length;
    const info: Record<string, unknown> = {
      path: this.tags.file,
      source: 'store',
      workbook_rows: items.length,
      matched_templates: items.length,
      source_ready_templates: ready,
      manifest_entries: manifest.items.length,
      manifest_unmatched: manifest.items.length - ready,
      untagged_skipped: untaggedCount,
      deprecated_skipped: deprecatedCount,
      unlisted_files: null,
      store_revision: this.tags.revision,
      all_untagged: items.length === 0,
      manifest_warning: manifest.error ? `云端模板索引不可用（${manifest.error}），本次仅按本地生产清单筛选` : '',
    };
    this.cache = { key, items, retired, untagged, error: '', info };
    this.logger.log(`模板候选集：${items.length} 条（云端就绪 ${ready}，未打标 ${untaggedCount}，弃用 ${deprecatedCount}）`);
  }

  /** 网页上传的视频不在云端 manifest 里，用本地 overlay 补 url/key（重启不丢）。 */
  private readOverlay(): ManifestEntry[] {
    const file = path.join(this.config.dataDir, 'manifest_overlay.json');
    try {
      if (!fs.existsSync(file)) return [];
      const data = JSON.parse(fs.readFileSync(file, 'utf8')) as unknown;
      const list = Array.isArray(data) ? data : ((data as { items?: unknown[] }).items ?? []);
      if (!Array.isArray(list)) return [];
      return list
        .map((item) => {
          const record = (item ?? {}) as Record<string, unknown>;
          const url = String(record.url ?? '').trim();
          const key = String(record.key ?? '').trim().replace(/^\/+/, '');
          if (!url && !key) return null;
          return { id: String(record.id ?? ''), url, key, ...record } as ManifestEntry;
        })
        .filter((item): item is ManifestEntry => item !== null);
    } catch (err) {
      this.logger.warn(`manifest_overlay 读取失败：${(err as Error).message}`);
      return [];
    }
  }

  private mergeSources(manifestItems: ManifestEntry[], overlay: ManifestEntry[]): ManifestEntry[] {
    const merged = [...manifestItems];
    const seen = new Set(manifestItems.map((item) => item.key || item.url).filter(Boolean));
    for (const item of overlay) {
      const marker = item.key || item.url;
      if (marker && seen.has(marker)) continue;
      merged.push(item);
      if (marker) seen.add(marker);
    }
    return merged;
  }

  private ensure(): CatalogState {
    return (
      this.cache ?? {
        key: '',
        items: [],
        retired: new Map(),
        untagged: new Map(),
        error: '模板清单尚未加载',
        info: {},
      }
    );
  }

  get error(): string {
    return this.ensure().error;
  }

  get info(): Record<string, unknown> {
    return this.ensure().info;
  }

  get items(): TemplateRow[] {
    return this.ensure().items;
  }

  get tagCategories(): string[] {
    const order = this.tags.load().library?.category_order ?? [];
    const fromStore = Object.values(this.tags.tagCategories);
    const list = (Array.isArray(order) && order.length ? [...order] : [...new Set(fromStore)]) as string[];
    return list.filter(Boolean);
  }

  /** 与 Python `_collect_tag_values` 等价：只保留候选集里实际出现过的标签。 */
  get tagValues(): Record<string, string[]> {
    const values: Record<string, string[]> = {};
    for (const category of this.tagCategories) values[category] = [];
    for (const item of this.items) {
      for (const tag of item.tags) {
        const category = item.tag_categories[tag] ?? '未分类';
        const bucket = (values[category] ||= []);
        if (!bucket.includes(tag)) bucket.push(tag);
      }
    }
    return values;
  }

  get tagFilterEnabled(): boolean {
    return Object.values(this.tagValues).some((list) => list.length > 0);
  }

  public(): PublicTemplate[] {
    return this.items.map((item) => ({
      id: item.id,
      name: item.name || `场景 ${item.id}`,
      shot: item.shot,
      duration: item.duration,
      size_mb: item.size_mb,
      tags: [...item.tags],
      tag_categories: { ...item.tag_categories },
      ready: Boolean(item.url || item.key),
      upload_state: item.upload_state,
    }));
  }

  /**
   * 标签页专用行：在用 + 未打标 + 已弃用**全量**，带弃用信息与 ready。
   *
   * 与 Python `TemplateCatalog.public_tags()` 逐字段对齐；返回顺序固定为
   * 「在用（候选集顺序）→ 未打标（按数字 ID 升序）→ 已弃用（按数字 ID 升序）」，
   * 前端直接按这个顺序渲染三段列表，不用自己排。
   */
  publicTags(): TagPublicRow[] {
    const payload = (item: TemplateRow): TagPublicRow => ({
      id: item.id,
      name: item.name || '',
      filename: item.filename || '',
      relative_path: item.relative_path || '',
      tags: [...item.tags],
      tag_categories: { ...item.tag_categories },
      deprecated: Boolean(item.deprecated),
      deprecated_at: item.deprecated_at,
      deprecated_by: item.deprecated_by || '',
      ready: Boolean(item.url || item.key),
      upload_state: item.upload_state || '',
      duration: item.duration,
      size_mb: item.size_mb,
      missing: Boolean(item.missing),
      changed: Boolean(item.changed),
      touched_at: item.touched_at,
      touched_by: item.touched_by || '',
      uploaded_at: Number(item.uploaded_at || 0),
      rating: Number(item.rating || 0),
    });
    const numericId = (row: TagPublicRow): number => (/^\d+$/.test(row.id) ? Number(row.id) : 0);
    const state = this.ensure();
    const rows = state.items.map(payload);
    const untagged = [...state.untagged.values()].map(payload).sort((a, b) => numericId(a) - numericId(b));
    const retired = [...state.retired.values()].map(payload).sort((a, b) => numericId(a) - numericId(b));
    return [...rows, ...untagged, ...retired];
  }

  /** 强制重建候选集（标签写成功后要立刻看到新行）。 */
  rebuild(): Promise<void> {
    return this.build(true);
  }

  resolve(templateId: string, includeRetired = false): TemplateRow {
    const id = normalizeId(templateId);
    const state = this.ensure();
    const hit = state.items.find((item) => item.id === id);
    if (hit) return hit;
    if (includeRetired) {
      const retired = state.retired.get(id) ?? state.untagged.get(id);
      if (retired) return retired;
    }
    throw new TemplateError('找不到该视频场景');
  }

  /** 标签筛选：同类 OR、跨类 AND；排除优先于包含。 */
  filterItems(tagFilters?: Record<string, string[]>, tagExcludes?: Record<string, string[]>): TemplateRow[] {
    const normalize = (raw: Record<string, string[]> | undefined, what: string): Record<string, Set<string>> => {
      if (!raw) return {};
      if (typeof raw !== 'object' || Array.isArray(raw)) throw new TemplateError(`标签${what}条件格式无效`);
      const out: Record<string, Set<string>> = {};
      for (const [category, values] of Object.entries(raw)) {
        if (!this.tagCategories.includes(category)) throw new TemplateError(`未知标签分类：${category}`);
        if (!Array.isArray(values)) throw new TemplateError(`${category} ${what}必须为标签列表`);
        const cleaned = [...new Set(values.map((v) => String(v ?? '').trim()).filter(Boolean))];
        if (!cleaned.length) continue;
        const allowed = new Set(this.tagValues[category] ?? []);
        const unknown = cleaned.find((value) => !allowed.has(value));
        if (unknown) throw new TemplateError(`${category} 中不存在标签：${unknown}`);
        out[category] = new Set(cleaned);
      }
      return out;
    };
    const include = normalize(tagFilters, '筛选');
    const exclude = normalize(tagExcludes, '排除');
    if (!Object.keys(include).length && !Object.keys(exclude).length) return [...this.items];
    const excluded = new Set<string>();
    for (const values of Object.values(exclude)) for (const tag of values) excluded.add(tag);
    return this.items.filter((item) => {
      if (excluded.size && item.tags.some((tag) => excluded.has(tag))) return false;
      for (const [category, values] of Object.entries(include)) {
        const hit = item.tags.some((tag) => values.has(tag) && item.tag_categories[tag] === category);
        if (!hit) return false;
      }
      return true;
    });
  }

  /**
   * 选片：`random`（自动推荐）/ `selected`（手动挑选，支持逗号、分号、空白分隔多编号）。
   *
   * 与 Python `TemplateCatalog.choose` 逐条对齐：随机模式下数量必须落在
   * `1..min(max_items, 候选数)`，越界报「当前筛选条件可生成 1–N 条」；
   * 其它模式一律「请选择自动推荐或手动挑选场景」——**没有 `first`**，别加回来。
   */
  choose(
    mode: string,
    count = 1,
    templateId = '',
    maxItems = 20,
    tagFilters?: Record<string, string[]>,
  ): TemplateRow[] {
    const candidates = this.filterItems(tagFilters);
    if (!candidates.length) throw new TemplateError('当前标签筛选没有匹配的视频场景');
    const normalized = (mode || 'random').toLowerCase();
    if (normalized === 'selected') {
      const wanted: string[] = [];
      for (const raw of String(templateId || '').split(/[,;，；\s]+/)) {
        const id = normalizeId(raw);
        if (id && !wanted.includes(id)) wanted.push(id);
      }
      if (!wanted.length) throw new TemplateError('请选择符合当前标签筛选的视频场景');
      const cap = Math.max(1, Math.min(maxItems, candidates.length));
      if (wanted.length > cap) throw new TemplateError(`一次最多挑选 ${cap} 个视频场景`);
      return wanted.map((id) => {
        const hit = candidates.find((item) => item.id === id);
        if (!hit) throw new TemplateError(`场景 ${id} 不在当前标签筛选结果里，请调整筛选后重选`);
        return hit;
      });
    }
    if (normalized !== 'random') throw new TemplateError('请选择自动推荐或手动挑选场景');
    const wanted = Number(count);
    const cap = Math.min(maxItems, candidates.length);
    if (!Number.isFinite(wanted) || wanted < 1 || wanted > cap) {
      throw new TemplateError(`当前筛选条件可生成 1–${cap} 条`);
    }
    const shuffled = [...candidates];
    for (let i = shuffled.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
    }
    return shuffled.slice(0, wanted);
  }
}
