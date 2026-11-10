/**
 * 标签唯一真源（`template_tags.json`）的读写门面。
 *
 * 逐条移植 `tools/template_store.py::TemplateStore`：字段名、错误文案、revision 乐观锁、
 * 审计上限、版本快照命名规则全部照抄，前端与 Python 版可以读同一份文件、互相接力。
 *
 * 与 Python 的差异只有「锁的实现」与「不移植 xlsx」两处，均在对应位置写明：
 * - 跨进程锁：Python 用 msvcrt/flock 锁 `<target>.lock` 的首字节；Node 没有内置跨进程
 *   文件锁，这里改成**原子创建目录** `<target>.lock/`——Windows 与 POSIX 上 `mkdir` 都是
 *   原子的，成功即拿到锁，失败就 50ms 后重试，5 秒超时。前提是 Python 与 Node 不会同时
 *   跑同一个 data_dir（任务书的口径：Node 侧接管后 Python 只作为只读参考）。
 * - `importFromXlsx` / `exportToXlsx` 不移植：xlsx 只在本机 CLI（`run_web.py --import-tags`）
 *   使用，网页端永远只读这份 JSON。
 *
 * 设计约定（与原实现一致）：
 * - 所有写入走 `mutate()`：进程内串行 → 跨进程锁 → **锁内重读** → 比对 revision → 改内存
 *   → revision+1 → 审计 → 原子写（tmp + rename）→ 自动版本快照。后写者拿旧 revision 会
 *   被拒（409）。
 * - 「标签为空」是未打标素材，「deprecated」是显式弃用；两者都不进候选集，但语义不同。
 * - 文件里**不存** url/key：云端地址永远由候选集按「文件名 / 相对路径 / 指纹 + 大小」现场 join。
 */
import * as fs from 'node:fs';
import * as zlib from 'node:zlib';
import { randomUUID } from 'node:crypto';
import { basename, dirname, join } from 'node:path';

import {
  TAG_CATEGORIES,
  TagNameError,
  normalizeId,
  normalizeTagCategories,
  rawText,
  validateTagName,
} from './tag-rules';

export { TAG_CATEGORIES, TagNameError, normalizeId, normalizeTagCategories, validateTagName };

/** 真源结构版本；比它更新的文件拒绝打开（防止新程序写的字段被老程序抹掉）。 */
export const SCHEMA_VERSION = 3;
/** 审计最多留 500 条，超出的从头部丢，丢了多少记在 `audit_dropped`。 */
export const AUDIT_LIMIT = 500;
/** 单次批量操作条数上限。 */
export const MAX_BULK_IDS = 2000;
/** 打标方式。 */
export const MODES = ['replace', 'add', 'remove'] as const;
/** 支持的影响预览动作。 */
export const LIBRARY_ACTIONS = ['create', 'rename', 'set_category', 'merge', 'delete'] as const;

/** 版本快照目录名（与真源同级的 `tag_versions/`）。 */
export const VERSION_DIRNAME = 'tag_versions';
export const VERSION_SCHEMA = 1;
/** 自动版本只留最近 50 份。 */
export const AUTO_KEEP = 50;
/** 手动版本 / 回滚前备份只留最近 50 份。 */
export const PINNED_KEEP = 50;
/** 版本文件名规则，必须与 Python 完全一致，否则两边互相读不到。 */
export const VERSION_NAME = /^v(\d{6})-(\d+)-(auto|manual)-([0-9a-f]{4})\.json\.gz$/;

// ---------------------------------------------------------------------- 错误类型

/** 参数/语义错误，映射 HTTP 400。 */
export class StoreError extends Error {
  readonly status: number;

  constructor(message: string, status = 400) {
    super(message);
    this.name = 'StoreError';
    this.status = status;
    // TS 编译到 ES5 时 `extends Error` 会丢原型，这里显式接回去（本项目 target 是 ES2022，
    // 留着也不会坏事，测试里的 instanceof 更稳）。
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** 找不到目标（视频条目 / 版本文件），映射 HTTP 404。 */
export class NotFound extends StoreError {
  constructor(message: string) {
    super(message, 404);
    this.name = 'NotFound';
  }
}

/** 乐观锁失败：调用方基于旧 revision 写入，映射 HTTP 409。 */
export class RevisionConflict extends StoreError {
  readonly revision: number;

  constructor(message: string, revision: number) {
    super(message, 409);
    this.name = 'RevisionConflict';
    this.revision = revision;
  }
}

/** 文件损坏、写入失败或锁超时，映射 HTTP 503。 */
export class StoreUnavailable extends StoreError {
  constructor(message: string, status = 503) {
    super(message, status);
    this.name = 'StoreUnavailable';
  }
}

// ---------------------------------------------------------------------- 数据类型

/** 视频条目：白名单字段与 Python `_normalize_video` 一一对应，额外字段原样保留。 */
export interface VideoRow {
  id: string;
  relative_path: string;
  name: string;
  duration: number | null;
  size_mb: number | null;
  size_bytes: number | null;
  fingerprint: string;
  tags: string[];
  deprecated: boolean;
  deprecated_at: number;
  deprecated_by: string;
  deprecated_reason: string;
  missing: boolean;
  changed: boolean;
  previous_fingerprint: string;
  uploaded: boolean;
  uploaded_at: number;
  source_dir: string;
  original_name: string;
  touched_at: number;
  touched_by: string;
  /** 评分 0-5；0 表示未打分。评分三件套必须进白名单，否则重载会剥掉评分。 */
  rating: number;
  rating_at: number;
  rating_by: string;
  [key: string]: unknown;
}

export interface StoreLibrary {
  version: number;
  tags: string[];
  categories: Record<string, string>;
  category_order: string[];
  [key: string]: unknown;
}

export interface StoreProject {
  project_id: string;
  source_root: string;
  roots: string[];
  next_id: number;
  [key: string]: unknown;
}

export interface AuditEntry {
  ts: number;
  user: string;
  revision: number;
  action: string;
  detail: Record<string, unknown>;
  [key: string]: unknown;
}

export interface StoreData {
  schema: number;
  revision: number;
  updated_at: number;
  updated_by: string;
  library: StoreLibrary;
  project: StoreProject;
  videos: VideoRow[];
  audit: AuditEntry[];
  audit_dropped: number;
  migrated_from?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface VersionPayload {
  schema: number;
  kind: string;
  action: string;
  note: string;
  revision: number;
  at: number;
  user: string;
  detail: Record<string, unknown>;
  library: StoreLibrary;
  project: StoreProject;
  videos: VideoRow[];
  [key: string]: unknown;
}

export interface VersionMeta {
  name: string;
  kind: string;
  action: string;
  note: string;
  revision: number;
  at: number;
  user: string;
  count: number;
  rated: number;
  bytes: number;
  [key: string]: unknown;
}

/** 写操作返回体；`detail` 只用于写审计与版本快照，对外响应里会被摘掉（与 Python 一致）。 */
export interface MutationResult {
  changed: number;
  updated?: VideoRow[];
  skipped?: string[];
  library?: StoreLibrary;
  affected?: number;
  becomes_untagged?: number;
  restored?: number;
  added_since?: number;
  removed_since?: number;
  backup?: string;
  row?: VideoRow;
  renamed?: boolean;
  detail?: Record<string, unknown>;
  revision?: number;
  [key: string]: unknown;
}

export interface WriteOptions {
  user?: string;
  revision?: number | null;
}

// ---------------------------------------------------------------------- 小工具

/** 默认真源路径：`OUTFIT_TAG_STORE` 优先，否则仓库下的 `web-data/template_tags.json`。 */
export function defaultStorePath(): string {
  const override = String(process.env.OUTFIT_TAG_STORE || '').trim();
  if (override) return override;
  return join(process.cwd(), 'web-data', 'template_tags.json');
}

/**
 * 文件指纹：`mtimeNs:size` 字符串。
 *
 * Python 用 `(st_mtime_ns, st_size)` 元组比较；Node 的 `mtimeMs` 只有毫秒精度，
 * 同一毫秒内的两次写入会被判成「没变」，所以这里用 `bigint` 选项拿纳秒。
 */
function statOf(file: string): string | null {
  try {
    const stat = fs.statSync(file, { bigint: true });
    return `${stat.mtimeNs}:${stat.size}`;
  } catch {
    return null;
  }
}

/** Python `float(x or 0)` 的近似：非有限数一律 0。 */
function num(value: unknown, fallback = 0): number {
  if (value === null || value === undefined || value === '') return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** Python `int()` 的近似；无法转换返回 null（调用方决定报什么文案）。 */
function intLike(value: unknown): number | null {
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'number') return Number.isFinite(value) ? Math.trunc(value) : null;
  if (typeof value === 'string') {
    const text = value.trim();
    if (!/^[+-]?\d+$/.test(text)) return null;
    const parsed = Number(text);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** 深拷贝：真源是纯 JSON 结构，`structuredClone` 比 JSON 往返更快也更安全。 */
export function clone<T>(value: T): T {
  return structuredClone(value);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

/** 取文件名主干（Python `Path(x).stem`）：`a/b/1400.mp4` → `1400`。 */
function stemOf(value: string): string {
  const base = String(value || '').replace(/\\/g, '/').split('/').pop() ?? '';
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(0, dot) : base;
}

/** 取扩展名（Python `Path(x).suffix`）：没有扩展名返回空串。 */
function suffixOf(value: string): string {
  const base = String(value || '').replace(/\\/g, '/').split('/').pop() ?? '';
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot) : '';
}

// ---------------------------------------------------------------------- 归一化

/** 标签列表清洗：字符串按 `;` 拆，去空、去重、保序。 */
export function cleanTags(values: unknown): string[] {
  const list = typeof values === 'string' ? values.split(';') : values;
  if (!Array.isArray(list) && !(list instanceof Set)) return [];
  const out: string[] = [];
  for (const value of Array.from(list as Iterable<unknown>)) {
    const label = String(value ?? '').trim();
    if (label && !out.includes(label)) out.push(label);
  }
  return out;
}

/** 视频编号列表清洗：单值包成数组，去空去重，超过 2000 条拒绝。 */
export function cleanIds(values: unknown): string[] {
  const list = typeof values === 'string' ? [values] : values;
  if (!Array.isArray(list) && !(list instanceof Set)) {
    throw new StoreError('请提供要操作的视频编号列表');
  }
  const out: string[] = [];
  for (const value of Array.from(list as Iterable<unknown>)) {
    const templateId = normalizeId(value);
    if (templateId && !out.includes(templateId)) out.push(templateId);
  }
  if (!out.length) throw new StoreError('请提供要操作的视频编号列表');
  if (out.length > MAX_BULK_IDS) throw new StoreError(`单次最多操作 ${MAX_BULK_IDS} 条视频`);
  return out;
}

/** 评分钳到 0-5：非法/缺失一律 0（未打分）。 */
export function clampRating(value: unknown): number {
  const rating = intLike(value);
  if (rating === null) {
    // Python 只对数字/数字字符串 `int()` 成功，`"3.5"` 会抛异常被吞成 0；这里等价处理
    if (typeof value === 'number' && Number.isFinite(value)) return Math.max(0, Math.min(5, Math.trunc(value)));
    return 0;
  }
  return Math.max(0, Math.min(5, rating));
}

/** 空真源骨架（与 Python `_empty_data()` 完全同构）。 */
export function emptyData(): StoreData {
  return {
    schema: SCHEMA_VERSION,
    revision: 0,
    updated_at: 0,
    updated_by: '',
    library: { version: 2, tags: [], categories: {}, category_order: [...TAG_CATEGORIES] },
    project: { project_id: '', source_root: '', roots: [], next_id: 1 },
    videos: [],
    audit: [],
    audit_dropped: 0,
  };
}

/**
 * 单条视频归一化。
 *
 * 白名单字段逐个洗；**白名单之外的字段原样保留**（任务书要求「未知字段不丢」，
 * Python 版会直接丢掉，这里刻意放宽，写回时不至于把别人加的自定义列抹掉）。
 */
export function normalizeVideo(raw: unknown): VideoRow {
  if (!isRecord(raw)) throw new StoreUnavailable('标签真源里的视频条目格式错误');
  const id = normalizeId(raw.id);
  if (!id) throw new StoreUnavailable('标签真源里存在没有模板ID的视频条目');
  const row: VideoRow = {
    ...raw,
    id,
    relative_path: rawText(raw.relative_path).replace(/\\/g, '/'),
    name: rawText(raw.name),
    duration: raw.duration === undefined ? null : (raw.duration as number | null),
    size_mb: raw.size_mb === undefined ? null : (raw.size_mb as number | null),
    size_bytes: raw.size_bytes === undefined ? null : (raw.size_bytes as number | null),
    fingerprint: rawText(raw.fingerprint).trim().toLowerCase(),
    tags: cleanTags(raw.tags),
    deprecated: Boolean(raw.deprecated),
    deprecated_at: num(raw.deprecated_at),
    deprecated_by: rawText(raw.deprecated_by),
    deprecated_reason: rawText(raw.deprecated_reason),
    missing: Boolean(raw.missing),
    changed: Boolean(raw.changed),
    previous_fingerprint: rawText(raw.previous_fingerprint),
    uploaded: Boolean(raw.uploaded),
    uploaded_at: num(raw.uploaded_at),
    source_dir: rawText(raw.source_dir),
    original_name: rawText(raw.original_name),
    touched_at: num(raw.touched_at),
    touched_by: rawText(raw.touched_by),
    rating: clampRating(raw.rating),
    rating_at: num(raw.rating_at),
    rating_by: rawText(raw.rating_by),
  };
  return row;
}

/** 视频排序键：先数字 ID 升序，非数字 ID 排后面（Python `(not isdigit, int, id)`）。 */
export function videoSortKey(row: { id: string }): [number, number, string] {
  const id = String(row.id ?? '');
  const digits = /^\d+$/.test(id);
  return [digits ? 0 : 1, digits ? Number(id) : 0, id];
}

/**
 * 整份真源归一化：老文件读进来补默认值，写回后字段不丢。
 *
 * 顶层未知字段同样保留（理由同 `normalizeVideo`）。
 */
export function normalizeData(raw: unknown): StoreData {
  if (!isRecord(raw)) throw new StoreUnavailable('标签真源格式错误：顶层应为 JSON 对象');

  const schemaRaw = raw.schema;
  let schema = SCHEMA_VERSION;
  if (schemaRaw !== undefined && schemaRaw !== null && schemaRaw !== false && schemaRaw !== 0 && schemaRaw !== '') {
    const parsed = intLike(schemaRaw);
    if (parsed === null) throw new StoreUnavailable('标签真源格式错误：schema 不是整数');
    schema = parsed;
  }
  if (schema > SCHEMA_VERSION) {
    throw new StoreUnavailable(`标签真源版本过新（schema ${schema}），请升级程序后再打开`);
  }

  const libraryRaw = isRecord(raw.library) ? raw.library : {};
  const tags: string[] = [];
  const tagKeys = new Set<string>();
  for (const value of asArray(libraryRaw.tags)) {
    let name: string;
    try {
      name = validateTagName(value);
    } catch {
      continue; // 非法标签名直接跳过，别让一处脏数据卡住整份真源
    }
    const key = name.toLowerCase();
    if (tagKeys.has(key)) continue;
    tagKeys.add(key);
    tags.push(name);
  }

  // 大类是可增删的列表；老文件没有 category_order 时以默认四类为起点
  const order: string[] = [];
  for (const value of asArray(libraryRaw.category_order)) {
    const name = String(value ?? '').trim();
    if (name && !order.includes(name)) order.push(name);
  }
  const savedCategories: Record<string, unknown> = isRecord(libraryRaw.categories) ? libraryRaw.categories : {};
  for (const value of Object.values(savedCategories)) {
    const name = String(value ?? '').trim();
    if (name && !order.includes(name)) order.push(name);
  }
  const finalOrder = order.length ? order : [...TAG_CATEGORIES];
  const categories = normalizeTagCategories(tags, savedCategories, finalOrder);

  const videos: VideoRow[] = [];
  const seenIds = new Set<string>();
  for (const item of asArray(raw.videos)) {
    const video = normalizeVideo(item);
    if (seenIds.has(video.id)) throw new StoreUnavailable(`标签真源里模板ID重复：${video.id}`);
    seenIds.add(video.id);
    // 字典里没有的标签不丢（可能是别人刚加的），但补齐分类，避免前端拿不到分类
    for (const tag of video.tags) {
      if (tag in categories) continue;
      tags.push(tag);
      categories[tag] = normalizeTagCategories([tag], null, finalOrder)[tag] ?? finalOrder[0];
    }
    videos.push(video);
  }
  videos.sort((a, b) => {
    const [a0, a1, a2] = videoSortKey(a);
    const [b0, b1, b2] = videoSortKey(b);
    if (a0 !== b0) return a0 - b0;
    if (a1 !== b1) return a1 - b1;
    return a2 < b2 ? -1 : a2 > b2 ? 1 : 0;
  });

  const projectRaw = isRecord(raw.project) ? raw.project : {};
  const maxNumericId = videos.reduce((max, row) => (/^\d+$/.test(row.id) ? Math.max(max, Number(row.id)) : max), 0);
  let nextId = intLike(projectRaw.next_id);
  if (nextId === null || !nextId) nextId = maxNumericId + 1;
  const project: StoreProject = {
    project_id: rawText(projectRaw.project_id),
    source_root: rawText(projectRaw.source_root),
    roots: asArray(projectRaw.roots).map((value) => String(value)).filter(Boolean),
    next_id: nextId,
  };

  const audit = asArray(raw.audit)
    .filter((entry): entry is AuditEntry => isRecord(entry))
    .slice(-AUDIT_LIMIT);

  return {
    ...raw,
    schema: SCHEMA_VERSION,
    revision: intLike(raw.revision) ?? 0,
    updated_at: num(raw.updated_at),
    updated_by: rawText(raw.updated_by),
    library: { version: 2, tags, categories, category_order: finalOrder },
    project,
    videos,
    audit,
    audit_dropped: intLike(raw.audit_dropped) ?? 0,
    migrated_from: isRecord(raw.migrated_from) ? raw.migrated_from : null,
  };
}

// ---------------------------------------------------------------------- 跨进程锁

/**
 * 跨进程互斥：靠「原子创建目录」实现。
 *
 * 为什么不是锁文件：Python 用 msvcrt/flock 锁 `<target>.lock` 的首字节，进程退出时由
 * 操作系统自动释放；Node 没有等价 API，随手实现的文件锁会在崩溃后永久卡住。
 * 目录创建在 Windows 与 POSIX 上都是原子操作（要么建成、要么 EEXIST），所以：
 * - 成功 = 拿到锁；
 * - EEXIST = 别人在写，50ms 后重试；
 * - 超过 5 秒 = 抛 StoreUnavailable（「标签真源正在被其他程序写入，请稍后重试」）；
 * - 用完 `rmdirSync` 删掉目录（空目录才能删，不会误删别人的东西）。
 *
 * 兼容性处理：Python 版留下的 `<target>.lock` 是**普通文件**且不会被删除，
 * 所以遇到「同名普通文件」时先 unlink 再重试一次，否则老环境第一次写就会超时。
 */
class MkdirLock {
  private readonly lockDir: string;
  private held = false;

  constructor(
    target: string,
    private readonly timeoutMs = 5000,
  ) {
    this.lockDir = `${target}.lock`;
  }

  get dir(): string {
    return this.lockDir;
  }

  async acquire(): Promise<void> {
    fs.mkdirSync(dirname(this.lockDir), { recursive: true });
    const deadline = Date.now() + this.timeoutMs;
    let retriedLegacyFile = false;
    for (;;) {
      try {
        fs.mkdirSync(this.lockDir);
        this.held = true;
        return;
      } catch (err) {
        const code = (err as NodeJS.ErrnoException).code;
        if (code !== 'EEXIST') {
          throw new StoreUnavailable(`标签真源加锁失败：${errorMessage(err)}`);
        }
        // 旧的 Python 锁文件（普通文件）会永远 EEXIST：清掉一次再试
        if (!retriedLegacyFile) {
          retriedLegacyFile = true;
          try {
            const stat = fs.statSync(this.lockDir);
            if (stat.isFile()) {
              fs.rmSync(this.lockDir, { force: true });
              continue;
            }
          } catch {
            continue; // 目录刚被别人删掉，直接重试
          }
        }
        if (Date.now() > deadline) {
          throw new StoreUnavailable('标签真源正在被其他程序写入，请稍后重试');
        }
        await sleep(50);
      }
    }
  }

  release(): void {
    if (!this.held) return;
    this.held = false;
    try {
      fs.rmdirSync(this.lockDir);
    } catch {
      // 目录已经不在了（被人手删/异常清理）：不算错误
    }
  }
}

// ---------------------------------------------------------------------- 真源门面

/** 标签真源的读写门面。所有写操作都带 revision 乐观锁。 */
export class TemplateStore {
  readonly path: string;
  data: StoreData;
  /** 最近一次读/写后的文件指纹，用于 `refreshIfChanged()` 与锁内重读的短路判断。 */
  private stat: string | null;
  private byId: Map<string, VideoRow>;
  private queue: Promise<void> = Promise.resolve();
  /** 是否从上一次的 `.bak` 恢复而来（对应 Python `recovered_from_bak`）。 */
  recoveredFromBak = false;

  constructor(target: string, data: StoreData, stat: string | null = null) {
    this.path = target;
    this.data = data;
    this.stat = stat;
    this.byId = new Map();
    this.indexLocked();
  }

  // ------------------------------------------------------------------ 装载

  /**
   * 读取真源。
   *
   * 文件不存在时：`createEmpty` 为真返回空库，否则抛 NotFound（Python 抛 FileNotFoundError）。
   * 文件损坏时先尝试 `.bak` 兜底（全站不至于直接 503），失败才报 503。
   */
  static load(target?: string, createEmpty = false): TemplateStore {
    const file = target || defaultStorePath();
    if (!fs.existsSync(file)) {
      if (!createEmpty) throw new NotFound(`标签真源不存在：${file}`);
      return new TemplateStore(file, emptyData(), null);
    }
    let raw: unknown;
    try {
      raw = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
    } catch (err) {
      const name = basename(file);
      const backup = `${file}.bak`;
      if (fs.existsSync(backup)) {
        try {
          const recovered = JSON.parse(fs.readFileSync(backup, 'utf8').replace(/^\uFEFF/, ''));
          if (isRecord(recovered)) {
            const store = new TemplateStore(file, normalizeData(recovered), null);
            store.recoveredFromBak = true;
            return store;
          }
        } catch {
          // 备份也坏了：走下面的统一错误
        }
      }
      throw new StoreUnavailable(
        `标签真源文件损坏：${name}（${errorMessage(err)}）。可把 ${name}.bak 改回原名后重试`,
      );
    }
    return new TemplateStore(file, normalizeData(raw), statOf(file));
  }

  /** 建一个空库（不落盘，第一次写入才生成文件）。 */
  static empty(target?: string): TemplateStore {
    return new TemplateStore(target || defaultStorePath(), emptyData(), null);
  }

  private indexLocked(): void {
    this.byId = new Map(this.data.videos.map((row) => [row.id, row]));
  }

  /** 别的进程写过就重读；返回是否真的重载了。 */
  refreshIfChanged(): boolean {
    if (statOf(this.path) === this.stat) return false;
    if (!fs.existsSync(this.path)) return false;
    try {
      const raw = JSON.parse(fs.readFileSync(this.path, 'utf8').replace(/^\uFEFF/, ''));
      this.data = normalizeData(raw);
    } catch {
      return false; // 正在被别人写/已损坏：保留内存里的旧版本，别把页面打崩
    }
    this.stat = statOf(this.path);
    this.indexLocked();
    return true;
  }

  /**
   * 锁内重读：保证看到别的进程最后一次写入。
   *
   * 文件不存在时保留内存现状（空库第一次写入就是这种情况）；文件存在但坏了才报 503。
   */
  private reloadLocked(): void {
    if (!fs.existsSync(this.path)) return;
    let raw: unknown;
    try {
      raw = JSON.parse(fs.readFileSync(this.path, 'utf8').replace(/^\uFEFF/, ''));
    } catch (err) {
      throw new StoreUnavailable(`标签真源读取失败：${errorMessage(err)}`);
    }
    this.data = normalizeData(raw);
    this.stat = statOf(this.path);
    this.indexLocked();
  }

  // ------------------------------------------------------------------ 属性

  get revision(): number {
    return intLike(this.data.revision) ?? 0;
  }

  get updatedAt(): number {
    return num(this.data.updated_at);
  }

  get updatedBy(): string {
    return rawText(this.data.updated_by);
  }

  get schema(): number {
    return intLike(this.data.schema) ?? SCHEMA_VERSION;
  }

  library(): StoreLibrary {
    const library = this.data.library;
    const order = Array.isArray(library.category_order) && library.category_order.length
      ? [...library.category_order]
      : [...TAG_CATEGORIES];
    return {
      version: 2,
      tags: [...library.tags],
      categories: { ...library.categories },
      category_order: order,
    };
  }

  /** 可增删的大类顺序表；标签编辑器、筛选器都按它渲染。 */
  categoryList(): string[] {
    const order = Array.isArray(this.data.library.category_order) ? [...this.data.library.category_order] : [];
    return order.length ? order : [...TAG_CATEGORIES];
  }

  private allowedCategories(): string[] {
    return this.categoryList();
  }

  categories(): Record<string, string> {
    return { ...this.data.library.categories };
  }

  rows(includeDeprecated = true): VideoRow[] {
    return this.data.videos
      .filter((row) => includeDeprecated || !row.deprecated)
      .map((row) => clone(row));
  }

  row(templateId: string): VideoRow | null {
    const hit = this.byId.get(normalizeId(templateId));
    return hit ? clone(hit) : null;
  }

  /** 每个标签被多少条视频用到（标签库里没用过的标签补 0）。 */
  usage(): Record<string, number> {
    const counts: Record<string, number> = {};
    for (const tag of this.data.library.tags) counts[tag] = 0;
    for (const row of this.data.videos) {
      for (const tag of row.tags) counts[tag] = (counts[tag] ?? 0) + 1;
    }
    return counts;
  }

  stats(): Record<string, number> {
    const videos = this.data.videos;
    const deprecated = videos.filter((row) => row.deprecated).length;
    const untagged = videos.filter((row) => !row.tags.length && !row.deprecated).length;
    return {
      total: videos.length,
      active: videos.length - deprecated - untagged,
      deprecated,
      untagged,
      missing: videos.filter((row) => row.missing).length,
      changed: videos.filter((row) => row.changed).length,
      uploaded: videos.filter((row) => row.uploaded).length,
    };
  }

  // ------------------------------------------------------------------ 写入骨架

  /** 进程内串行：同一实例上的写操作排队，避免自己跟自己抢文件锁。 */
  private async serial<T>(task: () => Promise<T> | T): Promise<T> {
    const before = this.queue;
    let release: () => void = () => undefined;
    this.queue = new Promise<void>((resolve) => {
      release = resolve;
    });
    await before.catch(() => undefined);
    try {
      return await task();
    } finally {
      release();
    }
  }

  /** 把 rename 的结果也刷到目录项上；Windows 不能 open 目录，跳过即可。 */
  private fsyncDir(): void {
    if (process.platform === 'win32') return;
    let fd = -1;
    try {
      fd = fs.openSync(dirname(this.path), 'r');
      fs.fsyncSync(fd);
    } catch {
      // 尽力而为：目录 fsync 失败不影响数据正确性
    } finally {
      if (fd >= 0) {
        try {
          fs.closeSync(fd);
        } catch {
          /* 忽略 */
        }
      }
    }
  }

  /** 原子写：旧文件先备份成 `.bak`，再写 `.tmp` → fsync → rename。 */
  private atomicSave(): void {
    fs.mkdirSync(dirname(this.path), { recursive: true });
    const payload = JSON.stringify(this.data);
    const tmp = `${this.path}.tmp`;
    try {
      if (fs.existsSync(this.path) && fs.statSync(this.path).size) {
        fs.copyFileSync(this.path, `${this.path}.bak`);
      }
      const fd = fs.openSync(tmp, 'w');
      try {
        fs.writeFileSync(fd, payload, 'utf8');
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(tmp, this.path);
      this.fsyncDir();
    } catch (err) {
      try {
        fs.rmSync(tmp, { force: true });
      } catch {
        /* 忽略 */
      }
      throw new StoreUnavailable(`标签真源写入失败：${errorMessage(err)}`);
    }
    this.stat = statOf(this.path);
  }

  /** 追加审计；超过 500 条从头部丢，溢出数累加到 `audit_dropped`。 */
  private auditAppend(action: string, user: string, detail: Record<string, unknown>): void {
    const entry: AuditEntry = {
      ts: Date.now() / 1000,
      user: String(user ?? ''),
      revision: this.revision,
      action: String(action ?? ''),
      detail: detail || {},
    };
    if (!Array.isArray(this.data.audit)) this.data.audit = [];
    this.data.audit.push(entry);
    const overflow = this.data.audit.length - AUDIT_LIMIT;
    if (overflow > 0) {
      this.data.audit.splice(0, overflow);
      this.data.audit_dropped = (intLike(this.data.audit_dropped) ?? 0) + overflow;
    }
  }

  /**
   * 统一写路径：串行 → 文件锁 → 重读 → 校验 revision → 改内存 → revision+1 → 审计 → 原子写。
   *
   * 写盘失败时把内存整体退回调用前，避免「接口 503、内存却已改」的幻影数据。
   */
  private async mutate(
    action: string,
    user: string,
    revision: number | null | undefined,
    op: () => MutationResult,
  ): Promise<MutationResult> {
    return this.serial(async () => {
      const lock = new MkdirLock(this.path);
      await lock.acquire();
      try {
        this.reloadLocked();
        if (revision !== undefined && revision !== null) {
          const expected = intLike(revision);
          if (expected === null) throw new StoreError('revision 必须是整数');
          if (expected !== this.revision) {
            throw new RevisionConflict('标签数据已被其他人更新，请刷新后重试', this.revision);
          }
        }
        // 快照必须是深拷贝：op 是原地改数组/对象的，留着引用等于没备份
        const snapshot = { data: clone(this.data), stat: this.stat };
        const result = op() ?? ({} as MutationResult);
        const changed = intLike(result.changed) ?? 0;
        const detail = isRecord(result.detail) ? result.detail : {};
        delete result.detail;
        if (!changed) {
          // 没有实质变化就整份退回：顺手抹掉 op 里可能写进内存的旁支字段，免得内存与磁盘分叉
          this.data = snapshot.data;
          this.stat = snapshot.stat;
          this.indexLocked();
          return { ...result, changed: 0, revision: this.revision };
        }
        this.data.revision = this.revision + 1;
        this.data.updated_at = Date.now() / 1000;
        this.data.updated_by = String(user ?? '');
        try {
          this.auditAppend(action, user, detail);
          this.atomicSave();
        } catch (err) {
          this.data = snapshot.data;
          this.stat = snapshot.stat;
          this.indexLocked();
          throw err;
        }
        this.indexLocked();
        // 每次成功写入都留一份整库快照；快照失败不影响已经落盘的数据
        try {
          this.writeVersionLocked('auto', action, user, detail);
        } catch {
          /* 版本是旁路能力，不能反过来阻断保存 */
        }
        return { ...result, changed, revision: this.revision };
      } finally {
        lock.release();
      }
    });
  }

  // ------------------------------------------------------------------ 视频写

  /** 改标签：replace 覆盖 / add 追加 / remove 删除。 */
  async setTags(ids: unknown, tags: unknown, options: WriteOptions & { mode?: string } = {}): Promise<MutationResult> {
    const mode = String(options.mode || 'replace').trim().toLowerCase();
    if (!(MODES as readonly string[]).includes(mode)) {
      throw new StoreError('打标方式只能是 replace / add / remove');
    }
    const wanted = cleanIds(ids);
    const labels = cleanTags(tags);

    return this.mutate('set_tags', String(options.user ?? ''), options.revision, () => {
      // 锁内重读后再校验：别人刚删掉的标签不能写进来
      const known = new Set(this.data.library.tags);
      const unknown = labels.filter((label) => !known.has(label));
      if (unknown.length) throw new StoreError(`标签不存在：${unknown[0]}`);

      const updated: VideoRow[] = [];
      const skipped: string[] = [];
      const now = Date.now() / 1000;
      for (const templateId of wanted) {
        const row = this.byId.get(templateId);
        if (!row) {
          skipped.push(templateId);
          continue;
        }
        const before = [...row.tags];
        let after: string[];
        if (mode === 'replace') {
          after = [...labels];
        } else if (mode === 'add') {
          after = [...before, ...labels.filter((label) => !before.includes(label))];
        } else {
          after = before.filter((label) => !labels.includes(label));
        }
        if (after.length === before.length && after.every((label, index) => label === before[index])) {
          continue;
        }
        row.tags = after;
        row.touched_at = now;
        row.touched_by = String(options.user ?? '');
        updated.push(clone(row));
      }
      if (!updated.length && skipped.length && skipped.length === wanted.length) {
        throw new NotFound(`视频不存在：${skipped[0]}`);
      }
      return {
        changed: updated.length,
        updated,
        skipped,
        detail: { ids: wanted, mode, tags: labels, affected: updated.length },
      };
    });
  }

  /** 弃用 / 恢复：软删除，行还在，随时可恢复。 */
  async deprecate(
    ids: unknown,
    deprecated: boolean,
    options: WriteOptions & { reason?: string } = {},
  ): Promise<MutationResult> {
    const wanted = cleanIds(ids);
    const flag = Boolean(deprecated);
    const reasonText = String(options.reason ?? '');

    return this.mutate('deprecate', String(options.user ?? ''), options.revision, () => {
      const updated: VideoRow[] = [];
      const skipped: string[] = [];
      const now = Date.now() / 1000;
      for (const templateId of wanted) {
        const row = this.byId.get(templateId);
        if (!row) {
          skipped.push(templateId);
          continue;
        }
        if (Boolean(row.deprecated) === flag) continue;
        row.deprecated = flag;
        row.deprecated_at = flag ? now : 0;
        row.deprecated_by = flag ? String(options.user ?? '') : '';
        row.deprecated_reason = flag ? reasonText : '';
        row.touched_at = now;
        row.touched_by = String(options.user ?? '');
        updated.push(clone(row));
      }
      if (!updated.length && skipped.length && skipped.length === wanted.length) {
        throw new NotFound(`视频不存在：${skipped[0]}`);
      }
      return {
        changed: updated.length,
        updated,
        skipped,
        detail: { ids: wanted, deprecated: flag, reason: reasonText },
      };
    });
  }

  /** 给视频打星：1-5 星，0 表示清除评分。打分也算一次修改，记 touched_at/审计。 */
  async setRating(ids: unknown, rating: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const wanted = cleanIds(ids);
    const value = intLike(rating);
    if (value === null) throw new StoreError('评分必须是 0-5 的整数');
    if (value < 0 || value > 5) throw new StoreError('评分必须是 0-5 的整数（0 为清除）');

    return this.mutate('set_rating', String(options.user ?? ''), options.revision, () => {
      const updated: VideoRow[] = [];
      const skipped: string[] = [];
      const now = Date.now() / 1000;
      for (const templateId of wanted) {
        const row = this.byId.get(templateId);
        if (!row) {
          skipped.push(templateId);
          continue;
        }
        const current = intLike(row.rating) ?? 0;
        if (current === value) continue;
        row.rating = value;
        row.rating_at = value ? now : 0;
        row.rating_by = value ? String(options.user ?? '') : '';
        row.touched_at = now;
        row.touched_by = String(options.user ?? '');
        updated.push(clone(row));
      }
      if (!updated.length && skipped.length && skipped.length === wanted.length) {
        throw new NotFound(`视频不存在：${skipped[0]}`);
      }
      return {
        changed: updated.length,
        updated,
        skipped,
        detail: { ids: wanted, rating: value, affected: updated.length },
      };
    });
  }

  /** 同一份清单里文件名必须唯一：重名自动加 -2、-3（COS 对象名另带随机后缀）。 */
  private uniqueNameLocked(name: string): string {
    const taken = new Set(this.data.videos.map((row) => String(row.name ?? '').toLowerCase()));
    const base = String(name ?? '').trim() || 'video.mp4';
    if (!taken.has(base.toLowerCase())) return base;
    const stem = stemOf(base);
    const suffix = suffixOf(base);
    let index = 2;
    while (taken.has(`${stem}-${index}${suffix}`.toLowerCase())) index += 1;
    return `${stem}-${index}${suffix}`;
  }

  /** 按清单里现有文件名的数字部分顺延，例如最后是 1411.mp4 → 1412。 */
  private nextMovieNumberLocked(): number {
    const numbers: number[] = [];
    for (const row of this.data.videos) {
      for (const raw of [row.name, row.relative_path, row.original_name]) {
        const stem = stemOf(String(raw ?? ''));
        if (/^\d+$/.test(stem)) numbers.push(Number(stem));
      }
    }
    return (numbers.length ? Math.max(...numbers) : 0) + 1;
  }

  /**
   * 登记一条新视频（编号自动顺延，重名自动改名）。
   *
   * `autoNumber` 为真时忽略原文件名，按清单里最后一个数字文件顺延成 0001 形式。
   * 本方法只写清单，不碰对象存储——COS 由调用方先传好再调这里。
   */
  async addVideo(options: {
    name: string;
    tags: unknown;
    sizeMb?: number;
    sizeBytes?: number;
    duration?: number | null;
    fingerprint?: string;
    uploaded?: boolean;
    sourceDir?: string;
    autoNumber?: boolean;
    user?: string;
    revision?: number | null;
  }): Promise<MutationResult> {
    const labels = cleanTags(options.tags);
    if (!labels.length) throw new StoreError('新视频至少要有一个标签');
    const rawName = String(options.name ?? '').trim() || 'video.mp4';
    const user = String(options.user ?? '');
    const uploaded = options.uploaded ?? true;
    const sourceDir = options.sourceDir ?? '网页上传';
    const autoNumber = Boolean(options.autoNumber);

    return this.mutate('upload_video', user, options.revision, () => {
      const known = new Set(this.data.library.tags);
      const unknown = labels.filter((label) => !known.has(label));
      if (unknown.length) throw new StoreError(`标签不存在：${unknown[0]}`);

      let filename: string;
      if (autoNumber) {
        const suffix = suffixOf(rawName).toLowerCase() || '.mp4';
        filename = this.uniqueNameLocked(`${String(this.nextMovieNumberLocked()).padStart(4, '0')}${suffix}`);
      } else {
        filename = this.uniqueNameLocked(rawName);
      }
      const videos = this.data.videos;
      const maximum = videos.reduce((max, row) => (/^\d+$/.test(row.id) ? Math.max(max, Number(row.id)) : max), 0);
      let nextId = intLike(this.data.project.next_id) ?? 0;
      nextId = Math.max(nextId, maximum + 1);
      const templateId = String(nextId).padStart(3, '0');
      const now = Date.now() / 1000;
      const row = normalizeVideo({
        id: templateId,
        relative_path: filename,
        name: filename,
        duration: options.duration ?? null,
        size_mb: options.sizeMb ?? 0,
        size_bytes: options.sizeBytes ?? 0,
        fingerprint: options.fingerprint ?? '',
        tags: labels,
        uploaded,
        deprecated: false,
        missing: false,
        changed: false,
        source_dir: sourceDir,
        uploaded_at: now,
        touched_at: now,
        touched_by: user,
      });
      videos.push(row);
      videos.sort((a, b) => {
        const [a0, a1, a2] = videoSortKey(a);
        const [b0, b1, b2] = videoSortKey(b);
        if (a0 !== b0) return a0 - b0;
        if (a1 !== b1) return a1 - b1;
        return a2 < b2 ? -1 : a2 > b2 ? 1 : 0;
      });
      this.data.project.next_id = nextId + 1;
      return {
        changed: 1,
        updated: [clone(row)],
        row: clone(row),
        renamed: filename !== rawName,
        detail: {
          id: templateId,
          name: filename,
          tags: labels,
          renamed_from: filename !== rawName ? rawName : '',
        },
      };
    });
  }

  // ------------------------------------------------------------------ 标签库写

  private requireTag(name: unknown): string {
    const label = String(name ?? '').trim();
    if (!label) throw new StoreError('请提供标签名称');
    if (!this.data.library.tags.includes(label)) throw new StoreError(`标签不存在：${label}`);
    return label;
  }

  /** 校验标签名并把 CatalogError 转成 StoreError（HTTP 层只认识 StoreError）。 */
  private tagName(value: unknown): string {
    try {
      return validateTagName(value);
    } catch (err) {
      if (err instanceof TagNameError) throw new StoreError(err.message);
      throw err;
    }
  }

  async libraryCreate(name: unknown, category = '', options: WriteOptions = {}): Promise<MutationResult> {
    const label = this.tagName(name);
    const wanted = String(category ?? '').trim();

    return this.mutate('library_create', String(options.user ?? ''), options.revision, () => {
      const allowed = this.allowedCategories();
      const target = wanted || (allowed.length ? allowed[0] : '场景');
      if (!allowed.includes(target)) throw new StoreError(`分类不存在：${target}（请先在标签库里新增大类）`);
      if (this.data.library.tags.some((tag) => tag.toLowerCase() === label.toLowerCase())) {
        throw new StoreError('这个标签已经存在');
      }
      this.data.library.tags.push(label);
      this.data.library.categories[label] = target;
      return { changed: 1, library: this.library(), affected: 0, detail: { name: label, category: target } };
    });
  }

  async libraryRename(name: unknown, newName: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const source = this.requireTag(name);
    const target = this.tagName(newName);
    if (target === source) throw new StoreError('新名称与原名相同');

    return this.mutate('library_rename', String(options.user ?? ''), options.revision, () => {
      const tags = this.data.library.tags;
      if (!tags.includes(source)) throw new StoreError(`标签不存在：${source}`);
      if (tags.some((tag) => tag.toLowerCase() === target.toLowerCase())) {
        throw new StoreError('新名称已存在，请改用合并');
      }
      tags[tags.indexOf(source)] = target;
      const categories = this.data.library.categories;
      const moved = categories[source] ?? '场景';
      delete categories[source];
      categories[target] = moved;
      const updated: VideoRow[] = [];
      for (const row of this.data.videos) {
        if (!row.tags.includes(source)) continue;
        row.tags = row.tags.map((tag) => (tag === source ? target : tag));
        updated.push(clone(row));
      }
      return {
        changed: 1,
        library: this.library(),
        affected: updated.length,
        updated,
        detail: { from: source, to: target, affected: updated.length },
      };
    });
  }

  async librarySetCategory(name: unknown, category: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const label = this.requireTag(name);
    const target = String(category ?? '').trim();

    return this.mutate('library_set_category', String(options.user ?? ''), options.revision, () => {
      if (!this.allowedCategories().includes(target)) {
        throw new StoreError(`分类不存在：${target}（请先在标签库里新增大类）`);
      }
      if (this.data.library.categories[label] === target) {
        return { changed: 0, library: this.library(), affected: 0 };
      }
      this.data.library.categories[label] = target;
      return { changed: 1, library: this.library(), affected: 0, detail: { name: label, category: target } };
    });
  }

  async libraryMerge(source: unknown, target: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const sourceLabel = this.requireTag(source);
    const targetLabel = this.requireTag(target);
    if (sourceLabel === targetLabel) throw new StoreError('不能把标签合并到自己');

    return this.mutate('library_merge', String(options.user ?? ''), options.revision, () => {
      const tags = this.data.library.tags;
      if (!tags.includes(sourceLabel) || !tags.includes(targetLabel)) {
        throw new StoreError('标签已被其他人删除，请刷新后重试');
      }
      const updated: VideoRow[] = [];
      for (const row of this.data.videos) {
        if (!row.tags.includes(sourceLabel)) continue;
        const merged: string[] = [];
        for (const tag of row.tags) {
          const label = tag === sourceLabel ? targetLabel : tag;
          if (!merged.includes(label)) merged.push(label);
        }
        row.tags = merged;
        updated.push(clone(row));
      }
      const index = this.data.library.tags.indexOf(sourceLabel);
      if (index >= 0) this.data.library.tags.splice(index, 1);
      delete this.data.library.categories[sourceLabel];
      return {
        changed: Math.max(1, updated.length),
        library: this.library(),
        affected: updated.length,
        updated,
        becomes_untagged: 0,
        detail: { source: sourceLabel, target: targetLabel, affected: updated.length },
      };
    });
  }

  async libraryDelete(name: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const label = this.requireTag(name);

    return this.mutate('library_delete', String(options.user ?? ''), options.revision, () => {
      const tags = this.data.library.tags;
      if (!tags.includes(label)) throw new StoreError(`标签不存在：${label}`);
      const updated: VideoRow[] = [];
      let becomesUntagged = 0;
      for (const row of this.data.videos) {
        if (!row.tags.includes(label)) continue;
        row.tags = row.tags.filter((tag) => tag !== label);
        if (!row.tags.length) becomesUntagged += 1;
        updated.push(clone(row));
      }
      const index = tags.indexOf(label);
      if (index >= 0) tags.splice(index, 1);
      delete this.data.library.categories[label];
      return {
        changed: Math.max(1, updated.length),
        library: this.library(),
        affected: updated.length,
        becomes_untagged: becomesUntagged,
        updated,
        detail: { name: label, affected: updated.length, becomes_untagged: becomesUntagged },
      };
    });
  }

  // ------------------------------------------------------------------ 大类（分类）

  /** 新增大类，例如「天气」「面料」。标签编辑器与筛选会立刻多出一栏。 */
  async categoryCreate(name: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const category = this.tagName(name);
    if ([...category].length > 20) throw new StoreError('大类名称最多 20 个字');

    return this.mutate('category_create', String(options.user ?? ''), options.revision, () => {
      const order = [...(this.data.library.category_order ?? [])];
      if (order.some((item) => item.toLowerCase() === category.toLowerCase())) {
        throw new StoreError('这个大类已经存在');
      }
      order.push(category);
      this.data.library.category_order = order;
      return { changed: 1, library: this.library(), affected: 0, detail: { category } };
    });
  }

  async categoryRename(name: unknown, newName: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const source = String(name ?? '').trim();
    const target = this.tagName(newName);
    if ([...target].length > 20) throw new StoreError('大类名称最多 20 个字');

    return this.mutate('category_rename', String(options.user ?? ''), options.revision, () => {
      const order = [...(this.data.library.category_order ?? [])];
      if (!order.includes(source)) throw new StoreError(`大类不存在：${source}`);
      if (order.some((item) => item.toLowerCase() === target.toLowerCase())) {
        throw new StoreError('这个大类已经存在');
      }
      order[order.indexOf(source)] = target;
      this.data.library.category_order = order;
      const categories = this.data.library.categories;
      let moved = 0;
      for (const [tag, category] of Object.entries(categories)) {
        if (category === source) {
          categories[tag] = target;
          moved += 1;
        }
      }
      return {
        changed: 1,
        library: this.library(),
        affected: moved,
        detail: { from: source, to: target, tags: moved },
      };
    });
  }

  /** 只允许删空大类；下面还有标签时要求先把标签移走或删掉。 */
  async categoryDelete(name: unknown, options: WriteOptions = {}): Promise<MutationResult> {
    const source = String(name ?? '').trim();

    return this.mutate('category_delete', String(options.user ?? ''), options.revision, () => {
      const order = [...(this.data.library.category_order ?? [])];
      if (!order.includes(source)) throw new StoreError(`大类不存在：${source}`);
      if (order.length <= 1) throw new StoreError('至少要保留一个大类');
      const used = Object.entries(this.data.library.categories)
        .filter(([, category]) => category === source)
        .map(([tag]) => tag);
      if (used.length) {
        throw new StoreError(`「${source}」下还有 ${used.length} 个标签（${used[0]}…），请先改分类或删除标签`);
      }
      order.splice(order.indexOf(source), 1);
      this.data.library.category_order = order;
      return { changed: 1, library: this.library(), affected: 0, detail: { category: source } };
    });
  }

  // ------------------------------------------------------------------ 只读推演

  /** 不写盘、不动 revision。给前端二次确认用。 */
  impactPreview(action: string, params: Record<string, unknown> = {}): Record<string, unknown> {
    const normalized = String(action ?? '').trim();
    const input = params ?? {};
    const rows = this.data.videos;
    let affected = 0;
    let becomesUntagged = 0;
    const samples: Array<{ id: string; name: string }> = [];

    if (['delete', 'merge', 'rename', 'set_category'].includes(normalized)) {
      const label = String(input.name ?? input.source ?? '').trim();
      if (label && !this.data.library.tags.includes(label)) throw new StoreError(`标签不存在：${label}`);
      if (label && ['delete', 'merge', 'rename'].includes(normalized)) {
        for (const row of rows) {
          if (!row.tags.includes(label)) continue;
          affected += 1;
          if (normalized === 'delete' && row.tags.length === 1) becomesUntagged += 1;
          if (samples.length < 5) samples.push({ id: row.id, name: row.name });
        }
      }
    } else if (normalized === 'deprecate') {
      for (const templateId of cleanIds(input.ids)) {
        const row = this.byId.get(templateId);
        if (!row || row.deprecated) continue;
        affected += 1;
        if (samples.length < 5) samples.push({ id: row.id, name: row.name });
      }
    } else if (normalized === 'set_tags' || normalized === 'library_create') {
      affected = input.ids ? cleanIds(input.ids).length : 0;
    } else {
      throw new StoreError('不支持的影响预览动作');
    }
    return { action: normalized, affected, becomes_untagged: becomesUntagged, samples, revision: this.revision };
  }

  auditTail(limit = 30): AuditEntry[] {
    const size = Math.max(1, Math.min(intLike(limit) || 30, AUDIT_LIMIT));
    return this.data.audit.slice(-size).map((entry) => ({ ...entry }));
  }

  // ------------------------------------------------------------------ 版本快照

  versionsDir(): string {
    return join(dirname(this.path), VERSION_DIRNAME);
  }

  /** 整份真源快照：视频库（含每条的标签与弃用态）+ 标签定义 + 项目信息。 */
  private snapshotPayload(
    kind: string,
    action: string,
    user: string,
    detail: Record<string, unknown>,
    note = '',
  ): VersionPayload {
    return {
      schema: VERSION_SCHEMA,
      kind,
      action: String(action ?? ''),
      note: String(note ?? '').slice(0, 120),
      revision: this.revision,
      at: Date.now() / 1000,
      user: String(user ?? ''),
      detail: detail || {},
      library: this.library(),
      project: clone(this.data.project ?? { project_id: '', source_root: '', roots: [], next_id: 1 }),
      videos: clone(this.data.videos ?? []),
    };
  }

  /**
   * 写一份 gzip 快照 + 同名 `.meta.json`，然后按种类淘汰旧版本。
   *
   * 格式必须与 Python 一致：`v{revision:06d}-{at}-{kind}-{4位hex}.json.gz`，
   * 内容是无 BOM 的 UTF-8 JSON（gzip 压缩级别 6），Python 用 `gzip.open` 直接能读。
   */
  private writeVersionLocked(
    kind: string,
    action: string,
    user: string,
    detail: Record<string, unknown>,
    note = '',
  ): VersionMeta {
    const payload = this.snapshotPayload(kind, action, user, detail, note);
    const folder = this.versionsDir();
    fs.mkdirSync(folder, { recursive: true });
    const name = `v${String(payload.revision).padStart(6, '0')}-${Math.trunc(payload.at)}-${kind}-${randomUUID()
      .replace(/-/g, '')
      .slice(0, 4)}.json.gz`;
    const target = join(folder, name);
    const tmp = `${target}.tmp`;
    const raw = Buffer.from(JSON.stringify(payload), 'utf8');
    fs.writeFileSync(tmp, zlib.gzipSync(raw, { level: 6 }));
    fs.renameSync(tmp, target);

    // 视频行是整行快照，评分随之入库；meta 里另记「该版本带多少条评分」，
    // 版本列表不解压快照就能看出评分覆盖度。
    const rated = payload.videos.filter((row) => (intLike(row.rating) ?? 0) > 0).length;
    const meta: VersionMeta = {
      name,
      kind,
      action: payload.action,
      note: payload.note,
      revision: payload.revision,
      at: payload.at,
      user: payload.user,
      count: payload.videos.length,
      rated,
      bytes: fs.statSync(target).size,
    };
    const metaPath = `${target}.meta.json`;
    const metaTmp = `${metaPath}.tmp`;
    fs.writeFileSync(metaTmp, JSON.stringify(meta), 'utf8');
    fs.renameSync(metaTmp, metaPath);
    this.pruneVersionsLocked();
    return meta;
  }

  /** 自动版本留最近 50 份、手动版本（含回滚前备份）留 50 份，其余连快照带 meta 一起删。 */
  private pruneVersionsLocked(): void {
    const folder = this.versionsDir();
    if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) return;
    const entries: Array<{ at: number; meta: Record<string, unknown>; metaPath: string }> = [];
    for (const fileName of fs.readdirSync(folder)) {
      if (!fileName.endsWith('.meta.json')) continue;
      const metaPath = join(folder, fileName);
      try {
        const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8')) as Record<string, unknown>;
        entries.push({ at: num(meta.at), meta, metaPath });
      } catch {
        continue; // 坏 meta 不参与淘汰，留给人工处理
      }
    }
    for (const [kind, keep] of [
      ['auto', AUTO_KEEP],
      ['manual', PINNED_KEEP],
    ] as Array<[string, number]>) {
      const group = entries.filter((item) => item.meta.kind === kind).sort((a, b) => a.at - b.at);
      for (const item of group.slice(0, Math.max(0, group.length - keep))) {
        const name = String(item.meta.name ?? '');
        if (name) {
          try {
            fs.rmSync(join(folder, name), { force: true });
          } catch {
            /* 删不掉就留着，下次再试 */
          }
        }
        try {
          fs.rmSync(item.metaPath, { force: true });
        } catch {
          /* 同上 */
        }
      }
    }
  }

  /** 只读 meta，不解析快照正文（快照是 gzip 的整库数据）。 */
  listVersions(limit = 100): VersionMeta[] {
    const size = Math.max(1, Math.min(intLike(limit) || 100, 500));
    const folder = this.versionsDir();
    if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) return [];
    const items: VersionMeta[] = [];
    for (const fileName of fs.readdirSync(folder)) {
      if (!fileName.endsWith('.meta.json')) continue;
      try {
        const meta = JSON.parse(fs.readFileSync(join(folder, fileName), 'utf8')) as VersionMeta;
        if (meta && typeof meta === 'object' && meta.name) items.push(meta);
      } catch {
        continue;
      }
    }
    items.sort((a, b) => num(b.at) - num(a.at));
    return items.slice(0, size);
  }

  /** 读一份快照正文；文件名不合法/缺失/损坏分别报不同错误。 */
  readVersion(name: string): VersionPayload {
    const rawName = String(name ?? '').trim();
    if (!VERSION_NAME.test(rawName)) throw new StoreError('版本文件名无效');
    const file = join(this.versionsDir(), rawName);
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) throw new NotFound('找不到该版本');
    let payload: unknown;
    try {
      payload = JSON.parse(zlib.gunzipSync(fs.readFileSync(file)).toString('utf8'));
    } catch (err) {
      throw new StoreUnavailable(`版本文件损坏：${rawName}（${errorMessage(err)}）`);
    }
    if (!isRecord(payload)) throw new StoreUnavailable(`版本文件格式错误：${rawName}`);
    return payload as VersionPayload;
  }

  /** 手动打一个版本（长期保留，不参与自动版本的 50 份淘汰）。 */
  async createVersion(note = '', options: WriteOptions = {}): Promise<VersionMeta> {
    return this.serial(async () => {
      const lock = new MkdirLock(this.path);
      await lock.acquire();
      try {
        this.reloadLocked();
        const text = String(note ?? '');
        return this.writeVersionLocked(
          'manual',
          'manual',
          String(options.user ?? '') || this.updatedBy,
          { note: text.slice(0, 120) },
          text,
        );
      } finally {
        lock.release();
      }
    });
  }

  /**
   * 把整份真源回滚到某个版本：视频库条目、每条标签、弃用态、标签定义一起还原。
   *
   * 回滚前会自动留一份 manual 备份（note=回滚前备份），所以这次回滚本身也能撤回。
   * 整库回滚不可增量合并，必须带 revision：否则两个人同时回滚会互相覆盖而不报错。
   */
  async restoreVersion(
    name: string,
    options: WriteOptions = {},
  ): Promise<MutationResult & { version: string }> {
    const revision = options.revision ?? null;
    if (revision === null || revision === undefined) {
      throw new StoreError('回滚整库必须带 revision：请先刷新拿到最新版本号再回滚');
    }
    const snapshot = this.readVersion(name);
    const rawVideos = snapshot.videos as unknown;
    if (!Array.isArray(rawVideos) || !rawVideos.length) throw new StoreError('该版本里没有视频数据，无法回滚');
    const videos: VideoRow[] = [];
    const seen = new Set<string>();
    for (const item of rawVideos) {
      const row = normalizeVideo(item);
      if (seen.has(row.id)) throw new StoreError(`版本数据里模板ID重复：${row.id}`);
      seen.add(row.id);
      videos.push(row);
    }

    const libraryRaw: Record<string, unknown> = isRecord(snapshot.library) ? snapshot.library : {};
    const targetTags = cleanTags(libraryRaw.tags);
    const targetOrder: string[] = [];
    for (const value of asArray(libraryRaw.category_order)) {
      const item = String(value ?? '').trim();
      if (item && !targetOrder.includes(item)) targetOrder.push(item);
    }
    const rawCategories: Record<string, unknown> = isRecord(libraryRaw.categories) ? libraryRaw.categories : {};
    for (const value of Object.values(rawCategories)) {
      const item = String(value ?? '').trim();
      if (item && !targetOrder.includes(item)) targetOrder.push(item);
    }
    const sortedOrder = targetOrder.length ? targetOrder : [...TAG_CATEGORIES];
    const targetCategories = normalizeTagCategories(targetTags, rawCategories, sortedOrder);
    const user = String(options.user ?? '');

    const result = await this.mutate('restore_version', user, revision, () => {
      // 先备份当前状态（与回滚同一次持锁，避免中间被别的进程插队）
      const backup = this.writeVersionLocked(
        'manual',
        'before_restore',
        user || this.updatedBy,
        { restore_to: name },
        `回滚前备份（${name}）`,
      );
      const currentIds = new Set((this.data.videos ?? []).map((row) => row.id));
      const targetIds = new Set(videos.map((row) => row.id));
      this.data.library = {
        version: 2,
        tags: targetTags,
        categories: targetCategories,
        category_order: sortedOrder,
      };
      this.data.videos = videos;
      const project = snapshot.project;
      if (isRecord(project) && Object.keys(project).length) this.data.project = clone(project) as StoreProject;
      return {
        changed: Math.max(1, videos.length),
        updated: [],
        restored: videos.length,
        // 当前有、目标版本没有 → 回滚后会从清单消失（数据仍在备份版里）
        added_since: [...currentIds].filter((id) => !targetIds.has(id)).length,
        // 目标版本有、当前没有 → 回滚后重新出现
        removed_since: [...targetIds].filter((id) => !currentIds.has(id)).length,
        backup: backup.name,
        detail: { restore_to: name, restored: videos.length },
      };
    });
    return { ...result, version: name };
  }
}
