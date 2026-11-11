/**
 * 「标签」页的 HTTP 业务层，逐条移植 `app/web/tag_api.py`。
 *
 * 权限口径（与 Python 一致）：**所有登录用户都能写**，不要求 admin；每次写都记审计。
 * 同步口径：不做轮询/SSE。前端进页面拉一次快照，保存后用响应里的 `updated` 行局部替换；
 * 并发靠 revision 乐观锁，旧值写入一律 409 并回带最新 revision。
 *
 * 错误一律用 `HttpException({ok:false, error, ...extra})` 抛出，让全局
 * `ApiExceptionFilter` 原样带出 `conflict` / `revision` / `impact` 这些附加字段。
 */
import { HttpException, Injectable, Logger } from '@nestjs/common';
import * as fs from 'node:fs';
import { randomUUID } from 'node:crypto';
import { basename, join } from 'node:path';

import type { AuthedUser } from '../auth/decorators';
import { AppConfigService } from '../config/app-config.service';
import { ManifestService } from '../templates/manifest.service';
import { TemplateCatalogService } from '../templates/template-catalog.service';
import { appendOverlay } from '../templates/overlay';
import { StorageService } from '../storage/storage.service';
import {
  AUTO_KEEP,
  PINNED_KEEP,
  RevisionConflict,
  StoreError,
  TemplateStore,
  type MutationResult,
  type StoreLibrary,
} from './store/template-store';
import { TemplateStoreService } from './store/template-store.service';

/** 单个视频上传上限（与 Python `tag_api.MAX_UPLOAD_MB` 一致）。 */
export const MAX_UPLOAD_MB = 50;
/** 允许的视频扩展名。 */
export const VIDEO_SUFFIXES = ['.mp4', '.mov', '.m4v'];

/** 标签库动作 → 允许的请求体字段（顺序即取值顺序，与 Python `_LIBRARY_PARAMS` 一致）。 */
const LIBRARY_PARAMS: Record<string, string[]> = {
  create: ['name', 'category'],
  rename: ['name', 'new_name'],
  set_category: ['name', 'category'],
  merge: ['source', 'target'],
  delete: ['name'],
  category_create: ['name'],
  category_rename: ['name', 'new_name'],
  category_delete: ['name'],
};

/** 需要二次确认的库级动作（会级联改写视频行）。 */
const CONFIRM_ACTIONS = ['merge', 'delete', 'rename'];

/** 上传请求的原始输入（控制器解完请求头后传进来）。 */
export interface UploadInput {
  data: Buffer;
  fileName: string;
  tags: string;
  autoNumber: boolean;
}

/** 取消 URL 编码；解不开就按原文返回（Python `urllib.parse.unquote` 不抛异常）。 */
function decodeHeader(value: unknown): string {
  const text = Array.isArray(value) ? String(value[0] ?? '') : String(value ?? '');
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

/** 取扩展名（小写，含点）；没有扩展名返回空串。 */
function extOf(name: string): string {
  const base = String(name || '').replace(/\\/g, '/').split('/').pop() ?? '';
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot).toLowerCase() : '';
}

@Injectable()
export class TagsService {
  private readonly logger = new Logger('Tags');

  constructor(
    private readonly config: AppConfigService,
    private readonly stores: TemplateStoreService,
    private readonly catalog: TemplateCatalogService,
    private readonly manifest: ManifestService,
    private readonly storage: StorageService,
  ) {}

  // ------------------------------------------------------------------ 工具

  /** 统一错误出口；extra 会原样进响应体（前端要拿 conflict/revision/impact）。 */
  private fail(message: string, status = 400, extra: Record<string, unknown> = {}): never {
    throw new HttpException({ ok: false, error: message, ...extra }, status);
  }

  /** StoreError → HTTP；非 StoreError 原样抛（交给全局过滤器当 500）。 */
  private rethrow(err: unknown): never {
    if (err instanceof RevisionConflict) {
      throw this.fail(err.message, 409, { conflict: true, revision: err.revision });
    }
    if (err instanceof StoreError) {
      throw this.fail(err.message, err.status || 400);
    }
    throw err;
  }

  /** 写接口必须带 revision：不带就等于关掉乐观锁，后写者会静默覆盖先写者。 */
  private revisionOf(body: Record<string, unknown>, action = '写入'): number {
    const raw = body.revision;
    if (raw === undefined || raw === null || typeof raw === 'boolean') {
      throw this.fail(`${action}需要 revision：请刷新页面拿到最新版本后重试`, 400);
    }
    let value: number;
    if (typeof raw === 'number' && Number.isFinite(raw)) {
      value = Math.trunc(raw);
    } else if (typeof raw === 'string' && /^[+-]?\d+$/.test(raw.trim())) {
      value = Number(raw.trim());
    } else {
      throw this.fail('revision 必须是整数：请刷新页面后重试', 400);
    }
    if (value < 0) throw this.fail('revision 不能为负数：请刷新页面后重试', 400);
    return value;
  }

  private requireStore(): TemplateStore {
    const store = this.stores.store;
    if (!store) {
      throw this.fail('标签真源尚未启用。请在主机执行：python run_web.py --import-tags', 503, {
        enabled: false,
        source: 'none',
      });
    }
    return store;
  }

  /** `stats` = 真源统计 + 候选集就绪数 + 候选条数（与 Python `_stats` 一致）。 */
  private statsOf(store: TemplateStore): Record<string, number> {
    const info = this.catalog.info;
    const stats = store.stats();
    stats.ready = Number(info.source_ready_templates ?? 0) || 0;
    stats.candidates = this.catalog.items.length;
    return stats;
  }

  /**
   * 写成功后的统一响应：先重建候选集（失败只记 error，不回滚已落盘的数据），
   * 再用 `catalog.publicTags()` 的 id 索引把 `updated` 行补上 ready/upload_state。
   */
  private async afterWrite(store: TemplateStore, result: MutationResult): Promise<Record<string, unknown>> {
    let error = '';
    try {
      await this.catalog.build(true);
    } catch (err) {
      error = (err as Error).message;
    }
    const payload: Record<string, unknown> = {
      ok: true,
      revision: Number(result.revision ?? store.revision),
      changed: Number(result.changed ?? 0),
      updated: this.tagRows(result.updated ?? []),
      stats: this.statsOf(store),
    };
    if ('library' in result && result.library) {
      payload.library = { ...(result.library as StoreLibrary), usage: store.usage() };
    }
    if ('affected' in result) payload.affected = Number(result.affected ?? 0);
    if ('becomes_untagged' in result) payload.becomes_untagged = Number(result.becomes_untagged ?? 0);
    if (result.skipped && result.skipped.length) payload.skipped = [...result.skipped];
    if (error) payload.catalog_error = `候选集重建失败：${error}`;
    return payload;
  }

  /** 用重建后的候选集行替换原始行；索引拿不到就退回原始行（Python `_rows`）。 */
  private tagRows(rows: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
    if (!rows.length) return [];
    let index = new Map<string, Record<string, unknown>>();
    try {
      index = new Map(this.catalog.publicTags().map((row) => [String(row.id), row as unknown as Record<string, unknown>]));
    } catch {
      index = new Map();
    }
    return rows.map((row) => index.get(String(row.id ?? '')) ?? row);
  }

  /** 未完成批次里还在等这些模板的提醒（只读扫 `batches/*.json`，不 import BatchesModule）。 */
  private lockedBatches(ids: string[]): Array<{ batch_no: string; items: number; state: string }> {
    const wanted = new Set(ids.map((value) => String(value ?? '')).filter(Boolean));
    if (!wanted.size) return [];
    const folder = join(this.config.dataDir, 'batches');
    let files: string[] = [];
    try {
      files = fs.readdirSync(folder).filter((name) => name.endsWith('.json') && !name.includes('.tmp'));
    } catch {
      return [];
    }
    const found: Array<{ batch_no: string; items: number; state: string }> = [];
    for (const name of files) {
      try {
        const batch = JSON.parse(fs.readFileSync(join(folder, name), 'utf8')) as Record<string, unknown>;
        if (!batch || typeof batch !== 'object') continue;
        if (batch.state === 'closed') continue;
        const items = Array.isArray(batch.items) ? (batch.items as Array<Record<string, unknown>>) : [];
        const pending = items.filter(
          (item) =>
            item &&
            ['pending', 'producing'].includes(String(item.state ?? '')) &&
            wanted.has(String(item.template_id ?? '')),
        );
        if (pending.length) {
          found.push({
            batch_no: String(batch.batch_no || batch.id || ''),
            items: pending.length,
            state: String(batch.state ?? ''),
          });
        }
      } catch {
        continue; // 坏文件跳过，别让一个坏批次挡住标签操作
      }
    }
    return found;
  }

  /** 库级操作影响的模板 ID：请求体里只有标签名，得按标签反查行。 */
  private libraryAffectedIds(store: TemplateStore, action: string, data: Record<string, unknown>): string[] {
    const names = new Set<string>();
    if (action === 'merge') names.add(String(data.source ?? '').trim());
    else if (['delete', 'rename', 'set_category'].includes(action)) names.add(String(data.name ?? '').trim());
    names.delete('');
    if (!names.size) return [];
    const ids: string[] = [];
    for (const row of store.rows(true)) {
      if (row.tags.some((tag) => names.has(String(tag)))) ids.push(String(row.id ?? ''));
    }
    return ids.filter(Boolean);
  }

  // ------------------------------------------------------------------ 读接口

  /**
   * 标签页快照。
   *
   * `rev` 命中当前 revision 时只回 `{ok, unchanged, revision}`，前端据此跳过整页重绘。
   * 桌面打标器可能刚写过：先按文件指纹判断要不要重载，再重建候选集。
   */
  async snapshot(rev?: string): Promise<Record<string, unknown>> {
    const store = this.stores.store;
    if (!store) {
      return {
        ok: true,
        enabled: false,
        source: 'none',
        revision: 0,
        videos: [],
        categories: [],
        library: { version: 2, tags: [], categories: {}, usage: {} },
        stats: {},
        audit: [],
        manifest: {},
        error: this.stores.error,
      };
    }

    const reloaded = this.stores.refreshIfChanged();
    try {
      await this.catalog.build(reloaded);
    } catch {
      /* 候选集建不出来也要能返回快照 */
    }

    const text = String(rev ?? '').trim();
    if (text && /^\d+$/.test(text) && Number(text) === store.revision) {
      return { ok: true, unchanged: true, revision: store.revision };
    }

    const info = this.catalog.info;
    const index = await this.manifest.load();
    return {
      ok: true,
      enabled: true,
      source: 'store',
      schema: store.schema,
      revision: store.revision,
      updated_at: store.updatedAt,
      updated_by: store.updatedBy,
      categories: this.catalog.tagCategories,
      library: { ...store.library(), usage: store.usage() },
      videos: this.catalog.publicTags(),
      stats: this.statsOf(store),
      audit: store.auditTail(30),
      manifest: {
        count: index.items.length,
        matched: Number(info.source_ready_templates ?? 0) || 0,
        pulled_at: Number(index.fetchedAt ?? 0) / 1000,
        warning: String(info.manifest_warning ?? ''),
      },
      path: store.path,
    };
  }

  /** 版本列表：整份真源快照（视频库 + 标签），自动 50 份 + 手动 50 份。 */
  versions(rawLimit?: string): Record<string, unknown> {
    const store = this.requireStore();
    const text = String(rawLimit ?? '').trim();
    // 与 server.py 一致：解析失败按 100 处理（不是报错）
    const limit = /^[+-]?\d+$/.test(text) ? Number(text) : 100;
    return {
      ok: true,
      revision: store.revision,
      versions: store.listVersions(limit),
      auto_keep: AUTO_KEEP,
      pinned_keep: PINNED_KEEP,
      path: store.versionsDir(),
    };
  }

  // ------------------------------------------------------------------ 写接口

  async setTags(body: Record<string, unknown>, user: AuthedUser): Promise<Record<string, unknown>> {
    const store = this.requireStore();
    const revision = this.revisionOf(body, '改标签');
    let result: MutationResult;
    try {
      result = await store.setTags(body.ids, body.tags, {
        mode: String(body.mode || 'replace'),
        user: user.username,
        revision,
      });
    } catch (err) {
      throw this.rethrow(err);
    }
    return this.afterWrite(store, result);
  }

  async deprecate(body: Record<string, unknown>, user: AuthedUser): Promise<Record<string, unknown>> {
    const store = this.requireStore();
    const revision = this.revisionOf(body, '弃用/恢复');
    let result: MutationResult;
    try {
      result = await store.deprecate(body.ids, Boolean(body.deprecated ?? true), {
        user: user.username,
        revision,
        reason: String(body.reason ?? ''),
      });
    } catch (err) {
      throw this.rethrow(err);
    }
    return this.afterWrite(store, result);
  }

  /** 给视频打星（1-5，0 清除）；走同一套 revision 乐观锁与审计。 */
  async setRating(body: Record<string, unknown>, user: AuthedUser): Promise<Record<string, unknown>> {
    const store = this.requireStore();
    const revision = this.revisionOf(body, '打分');
    let result: MutationResult;
    try {
      result = await store.setRating(body.ids, body.rating, { user: user.username, revision });
    } catch (err) {
      throw this.rethrow(err);
    }
    return this.afterWrite(store, result);
  }

  async library(body: Record<string, unknown>, user: AuthedUser): Promise<Record<string, unknown>> {
    const store = this.requireStore();
    const action = String(body.action ?? '').trim();
    const keys = LIBRARY_PARAMS[action];
    if (!keys) throw this.fail('不支持的标签库操作', 400);
    const revision = this.revisionOf(body, '标签库操作');
    const params: Record<string, unknown> = {};
    for (const key of keys) {
      if (key in body) params[key] = body[key];
    }

    // 合并/删除/改名会级联改写视频行：必须先让用户看到影响面再确认
    let impact: Record<string, unknown> = {};
    if (CONFIRM_ACTIONS.includes(action)) {
      try {
        impact = store.impactPreview(action, params);
      } catch (err) {
        throw this.rethrow(err);
      }
      const affected = Number(impact.affected ?? 0);
      if (affected > 0 && body.confirm !== true) {
        throw this.fail(`该操作将影响 ${affected} 条视频，请确认后再提交`, 400, {
          impact,
          confirm_required: true,
        });
      }
    }

    const username = user.username;
    let result: MutationResult;
    try {
      if (action === 'create') {
        result = await store.libraryCreate(params.name, String(params.category ?? ''), { user: username, revision });
      } else if (action === 'rename') {
        result = await store.libraryRename(params.name, params.new_name ?? '', { user: username, revision });
      } else if (action === 'set_category') {
        result = await store.librarySetCategory(params.name, String(params.category ?? ''), {
          user: username,
          revision,
        });
      } else if (action === 'merge') {
        result = await store.libraryMerge(params.source, params.target ?? '', { user: username, revision });
      } else if (action === 'category_create') {
        result = await store.categoryCreate(params.name ?? '', { user: username, revision });
      } else if (action === 'category_rename') {
        result = await store.categoryRename(params.name ?? '', params.new_name ?? '', { user: username, revision });
      } else if (action === 'category_delete') {
        result = await store.categoryDelete(params.name ?? '', { user: username, revision });
      } else {
        result = await store.libraryDelete(params.name, { user: username, revision });
      }
    } catch (err) {
      throw this.rethrow(err);
    }
    const payload = await this.afterWrite(store, result);
    payload.action = action;
    return payload;
  }

  /** 影响预览（不写盘、不动 revision）：给前端二次确认用。 */
  impact(body: Record<string, unknown>, _user: AuthedUser): Record<string, unknown> {
    const store = this.requireStore();
    const action = String(body.action ?? '').trim();
    let preview: Record<string, unknown>;
    try {
      preview = store.impactPreview(action, body);
    } catch (err) {
      throw this.rethrow(err);
    }
    if (action === 'deprecate') {
      const ids = Array.isArray(body.ids) ? body.ids.map((value) => String(value)) : [];
      preview.locked_batches = this.lockedBatches(ids);
    } else if (['merge', 'delete', 'rename', 'set_category'].includes(action)) {
      // 库级操作没有 ids：按标签反查受影响的行，才能提示「还有批次在等这条素材」
      preview.locked_batches = this.lockedBatches(this.libraryAffectedIds(store, action, body));
    }
    preview.ok = true;
    preview.checked_at = Date.now() / 1000;
    return preview;
  }

  /** 手动打一个版本（长期保留，不参与自动版本的 50 份淘汰）。 */
  async createVersion(body: Record<string, unknown>, user: AuthedUser): Promise<Record<string, unknown>> {
    const store = this.requireStore();
    const note = String(body.note ?? '').trim();
    try {
      const meta = await store.createVersion(note, { user: user.username });
      return { ok: true, version: meta, revision: store.revision };
    } catch (err) {
      throw this.rethrow(err);
    }
  }

  /** 回滚整份真源到指定版本；回滚前服务端会自动留一份「回滚前备份」。 */
  async restoreVersion(body: Record<string, unknown>, user: AuthedUser): Promise<Record<string, unknown>> {
    const store = this.requireStore();
    const name = String(body.name ?? '').trim();
    if (!name) throw this.fail('请指定要回滚的版本', 400);
    const revision = this.revisionOf(body, '回滚版本');
    let result: MutationResult & { version?: string };
    try {
      result = await store.restoreVersion(name, { user: user.username, revision });
    } catch (err) {
      throw this.rethrow(err);
    }
    const payload = await this.afterWrite(store, result);
    payload.restored = Number(result.restored ?? 0);
    payload.added_since = Number(result.added_since ?? 0);
    payload.removed_since = Number(result.removed_since ?? 0);
    payload.backup = String(result.backup ?? '');
    payload.version = name;
    return payload;
  }

  // ------------------------------------------------------------------ 上传新视频

  /**
   * 一次一条：校验 → 暂存 → 登记真源 → 重建候选集。
   *
   * 与 Python 的差异：COS 适配器还没接（P4），这里只把字节暂存到 `<dataDir>/tmp/` 并登记清单，
   * **不伪造 key/url**、不上传云端，响应里带 `warning` 说明。校验顺序与文案仍照
   * `server.py::_upload_template` + `tag_api.upload_video`。
   */
  async uploadVideo(input: UploadInput, user: AuthedUser): Promise<Record<string, unknown>> {
    const data = Buffer.isBuffer(input.data) ? input.data : Buffer.alloc(0);
    if (!data.length) throw this.fail('请选择要上传的视频文件。', 400);
    if (data.length > MAX_UPLOAD_MB * 1024 * 1024) {
      throw this.fail(`单个视频不能超过 ${MAX_UPLOAD_MB}MB。`, 413);
    }
    const fileName = basename(input.fileName || '') || 'video.mp4';
    const suffix = extOf(fileName);
    if (!VIDEO_SUFFIXES.includes(suffix)) {
      throw this.fail(`只支持 mp4 / mov / m4v 视频（收到 ${suffix || '无扩展名'}）。`, 400);
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(decodeHeader(input.tags) || '[]');
    } catch {
      throw this.fail('标签参数无效。', 400);
    }
    if (!Array.isArray(parsed)) throw this.fail('标签参数无效。', 400);

    const store = this.requireStore();
    const labels = (parsed as unknown[]).map((value) => String(value ?? '').trim()).filter(Boolean);
    if (!labels.length) throw this.fail('请至少选择一个标签', 400);
    const known = new Set(store.library().tags);
    const unknown = labels.find((label) => !known.has(label));
    if (unknown) throw this.fail(`标签不存在：${unknown}`, 400);

    if (!this.config.cosEnabled) {
      throw this.fail('云端模板库未启用（COS 未配置），无法上传视频', 503);
    }

    let result: MutationResult;
    try {
      result = await store.addVideo({
        name: fileName,
        tags: labels,
        sizeMb: Math.round((data.length / 1024 / 1024) * 1000) / 1000,
        sizeBytes: data.length,
        duration: null,
        fingerprint: '',
        uploaded: true,
        sourceDir: '网页上传',
        autoNumber: input.autoNumber,
        user: user.username,
      });
    } catch (err) {
      throw this.rethrow(err);
    }

    // 登记成功后按最终文件名传到 COS，再写 overlay 补 url/key（与 Python 同一套 Key：
    // `input/tpl/<文件名>`）。上传失败不回滚登记：视频行会显示「待上传」，可重传。
    const row = (result.row ?? {}) as Record<string, unknown>;
    const finalName = String(row.name || fileName);
    const key = `input/tpl/${finalName}`;
    let warning = '';
    try {
      await this.storage.putBuffer(data, key, this.storage.contentType(finalName));
      appendOverlay(this.config.dataDir, {
        id: String(row.id || ''),
        name: finalName,
        filename: finalName,
        relative_path: String(row.relative_path || finalName),
        key,
        size_mb: Math.round((data.length / 1024 / 1024) * 1000) / 1000,
        tags: labels,
        duration: null,
        uploaded: true,
      });
    } catch (err) {
      this.logger.warn(`视频已登记但上传云端失败：${(err as Error).message}`);
      warning = `视频已登记，但上传到云端失败：${(err as Error).message}`;
    }

    const payload = await this.afterWrite(store, result);
    payload.row = row;
    payload.renamed = Boolean(result.renamed);
    payload.auto_number = Boolean(input.autoNumber);
    if (warning) payload.warning = warning;
    return payload;
  }
}
