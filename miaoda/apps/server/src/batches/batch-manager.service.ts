/**
 * 任务批次：一份锁定清单 + 冻结点画质/时长，提示词锁定后仍可单独修改，之后分批生产。
 * 1:1 移植自 `app/web/batches.py`（1522 行）的 `BatchManager`。
 *
 * 核心约束（与 batch-spec.md 一致）：
 *   · 一次只做一件事：`draft`（挑清单）→ `locked`（可生产）→ `closed`。
 *   · **没有 running 态**：「是否在跑」由 `active_job_id` 派生，避免状态与 job 不同步。
 *   · 去重靠 `items[].state` 状态位，不反查 jobs——反查既慢，又会丢掉「还没生产的候选」。
 *   · 取数 + 标 producing + 建 job 必须在**同一个临界区**内。
 *   · 读时对账：`active_job_id` 对应的 job 已终态 → 收敛条目状态并清空 `active_job_id`。
 *     服务重启后首次读取即自动恢复，不引事件总线。
 *   · 存储：web-data/batches/<id>.json，一 batch 一文件（tmp + rename 原子写），
 *     两个批次并发生产互不覆盖。
 *   · 删除批次只删这一份批次 JSON：**已生成的视频属于 job**，output/ 与 jobs/ 下的文件
 *     一律不碰；job 的 batch_ref 保持原值不回写，历史分组按「批次文件还在不在」判定归属，
 *     删掉后这些 job 自然落回 __ungrouped__。
 *
 * 与 Python 的并发差异（重要）：
 *   Python 复用 `JobManager.lock`（RLock），批次与任务共用同一把临界区，因此「读批次 →
 *   jobs.create → 标 producing」这一步在跨线程时是原子的。Nest 侧 `JobManagerService`
 *   没有公开锁，Node 又是单线程，所以这里用一个只做「排队」的 `AsyncMutex`：
 *   **每个公开方法把「读-改-写」放在一个不含 `await` 的同步临界区里**，天然不会被别的
 *   请求插进来，语义与 Python 的 RLock 一致（`produce` 需要的 `catalog.build()` 是唯一
 *   的 await，它被刻意放在进入临界区**之前**）。`AsyncMutex` 不可重入：私有方法一律不取锁。
 */
import { Injectable, Logger, type OnApplicationBootstrap } from '@nestjs/common';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';

import {
  BATCH_STATES,
  DAY_KEY,
  DEFAULT_DURATION,
  DEFAULT_PRODUCE_COUNT,
  DEFAULT_TEMPLATE_SECONDS,
  ITEM_STATES,
  MAX_IMAGE_SLOTS,
  OUTPUT_SECONDS,
  OWNER_KEY,
  PRODUCE_CHOICES,
  PRODUCE_MIN,
  RETRY_MAX,
  RETRY_MIN,
  UNGROUPED_KEY,
  UPLOAD_PATH_RE,
  type BatchState,
  type ItemState,
} from '@outfit/shared';

import type { AuthedUser } from '../auth/decorators';
import { relocate } from '../common/paths';
import { AppConfigService } from '../config/app-config.service';
import { JobManagerService, type Job } from '../jobs/job-manager.service';
import { needsRecheck, type JobRecord } from '../jobs/job-runner';
import { zipFiles } from '../jobs/zip';
import { PromptsService } from '../templates/prompts.service';
import {
  TemplateCatalogService,
  TemplateError,
  type TemplateRow,
} from '../templates/template-catalog.service';
import { UploadsService } from '../uploads/uploads.service';
import { StyleUsageLedger } from './style-usage';

// ---------------------------------------------------------------- 与 Python 同口径的常量

const TERMINAL_JOB = new Set(['succeeded', 'partial', 'failed', 'canceled', 'interrupted']);
/** 记录里这些状态算「跑成了」。 */
const RECORD_DONE = new Set(['success']);
/** 记录里这些状态算「没跑成但也没花掉」：条目回 pending，可以再排一次。 */
const RECORD_UNTOUCHED = new Set(['pending', 'skipped']);
/**
 * 统计口径里的「失败侧」：非成功终态全部计入，保证 total = success + failed + unfinished。
 * unknown（提交结果未知/停止等待）云端可能已建任务并计费，必须算失败侧而非静默消失。
 */
const FAILED_RECORD_STATES = new Set([
  'failed',
  'invalid',
  'expired',
  'canceled',
  'unknown',
  'timeout',
  'skipped',
]);

/** 6 个固定标签栏位（与 `app/core.py::TAG_SLOTS` 逐字一致）。 */
const TAG_SLOTS: ReadonlyArray<readonly [string, string]> = [
  ['top', '上衣'],
  ['lining', '内衬'],
  ['pants', '裤子'],
  ['shoes', '鞋子'],
  ['scene', '场景'],
  ['note', '特殊备注'],
];
const TAG_LABELS: Record<string, string> = Object.fromEntries(TAG_SLOTS);
const TAG_CODE_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;
const STYLE_MAX = 40;
const NOTE_MAX = 100;

// ---------------------------------------------------------------- 类型

export interface BatchImageSlot {
  upload_id: string;
  name: string;
  path: string;
}

export interface BatchItem {
  template_id: string;
  name: string;
  tags: string[];
  state: ItemState;
  job_id: string;
  error: string;
  /** 云端已出片、只是本机下载失败：重试必须跳过（重试 = 二次提交 = 二次计费）。 */
  download_failed?: boolean;
}

export interface BatchRun {
  job_id: string;
  count: number;
  started_at: number;
  finished_at: number | null;
  success: number;
  failed: number;
  /** 对账收尾时被 pop 掉（加进 failed），所以落盘后可能没有这个字段。 */
  blocked?: number;
}

export interface BatchPendingRun {
  template_ids: string[];
  job_id: string;
  started_at: number;
}

/** 一 batch 一文件；**字段名与 Python 版完全一致**，历史批次能直接读。 */
export interface Batch {
  id: string;
  batch_no: string;
  title: string;
  owner: string;
  style_no: string;
  tags: Record<string, string>;
  state: BatchState;
  images: BatchImageSlot[];
  image_upload_id: string;
  image_path: string;
  image_name: string;
  prompt_id: string;
  prompt: string;
  resolution: string;
  duration: number;
  tag_filters: Record<string, string[]>;
  tag_excludes: Record<string, string[]>;
  items: BatchItem[];
  runs: BatchRun[];
  active_job_id: string;
  total_items: number;
  done: number;
  failed: number;
  pending: number;
  producing: number;
  created_at: number;
  updated_at: number;
  locked_at: number | null;
  closed_at: number | null;
  pending_run?: BatchPendingRun;
}

/** `public()` 下发的字段白名单（顺序与 Python 一致）。 */
const PUBLIC_KEYS = [
  'id',
  'batch_no',
  'title',
  'owner',
  'state',
  'image_upload_id',
  'image_name',
  'prompt_id',
  'prompt',
  'resolution',
  'duration',
  'tag_filters',
  'tag_excludes',
  'style_no',
  'tags',
  'total_items',
  'done',
  'failed',
  'pending',
  'producing',
  'active_job_id',
  'created_at',
  'updated_at',
  'locked_at',
  'closed_at',
] as const;

export type BatchView = Record<string, unknown>;

export interface BatchItemView extends BatchItem {
  download_url: string;
  verify_warn: string;
}

export interface CostEstimate {
  count: number;
  seconds: number;
  unit_price: number | null;
  price_source: string;
  amount: number | null;
  resolution: string;
  options: Array<{ count: number; amount: number | null }>;
  currency: string;
  note: string;
}

export interface BatchRunView {
  no: number;
  job_id: string;
  count: number;
  started_at: number;
  finished_at: number;
  success: number;
  failed: number;
  prompt: string;
  prompt_changed: boolean;
}

export interface HistoryGroupRow {
  key: string;
  day: string;
  batch_no: string;
  group_kind: 'batch' | 'owner' | 'day';
  is_ungrouped: boolean;
  small_jobs: number;
  big_jobs: number;
  owner: string;
  job_count: number;
  total: number;
  success: number;
  failed: number;
  unfinished: number;
  state_sig: string;
  first_at: number | null;
  last_at: number;
}

export interface HistoryRow {
  job_id: string;
  template_id: string;
  run_no: number;
  run_nos: number[];
  run_total: number;
  run_prompt_changed: boolean;
  image_url: string;
  template_name: string;
  owner: string;
  created_at: number;
  batch_no: string;
  job_status: string;
  status: string;
  seconds: number | null;
  download_url: string;
  archive_warning: string;
  verify_warn: string;
}

export interface HistoryItemsResult {
  key: string;
  batch_no: string;
  items: HistoryRow[];
  runs: BatchRunView[];
  total: number;
  offset: number;
  limit: number;
}

export interface BatchVideoInfo {
  id: string;
  batch_no: string;
  /** `[zip 内条目名, 本地绝对路径]`。 */
  files: Array<[string, string]>;
}

/** 带 HTTP 状态码的业务错误；controller 直接按 status 返回 `{ok:false,error}`。 */
export class BatchError extends Error {
  readonly status: number;

  constructor(message: string, status = 400) {
    super(message);
    this.name = 'BatchError';
    this.status = Number(status) || 400;
  }
}

// ---------------------------------------------------------------- 纯函数工具

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function nowSeconds(): number {
  return Date.now() / 1000;
}

/** `float(str(value).strip())`，非数字或 <=0 返回 null（对齐 `batches._as_float`）。 */
function asFloat(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const number = Number(String(value).trim());
  if (!Number.isFinite(number) || number <= 0) return null;
  return number;
}

/** `int(str(value).strip())`：只认整数写法（"3.0" 在 Python 里也是 ValueError）。 */
function asInt(value: unknown): number | null {
  const text = String(value ?? '').trim();
  if (!/^[+-]?\d+$/.test(text)) return null;
  const number = Number(text);
  return Number.isSafeInteger(number) ? number : null;
}

function asIntOr(value: unknown, fallback: number): number {
  const number = asInt(value);
  return number === null ? fallback : number;
}

/**
 * Python `round()`：四舍六入五取偶（银行家舍入）。
 * 金额/秒数直接决定界面数字，尽量贴住 CPython 的口径；二进制 .5 的极端情形按「就近」处理。
 */
function pyRound(value: number, digits: number): number {
  if (!Number.isFinite(value)) return value;
  const factor = 10 ** digits;
  const scaled = value * factor;
  const lower = Math.floor(scaled);
  const diff = scaled - lower;
  const rounded = Math.abs(diff - 0.5) < 1e-9 ? (lower % 2 === 0 ? lower : lower + 1) : Math.round(scaled);
  return rounded / factor;
}

/** Python `%g`（默认 6 位有效数字，去掉尾随 0）：7.0 → "7"，2.5 → "2.5"。 */
function formatG(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  return String(Number(value.toPrecision(6)));
}

/** 批次号唯一性比较忽略大小写与首尾空白。 */
export function normNo(value: unknown): string {
  return String(value ?? '')
    .trim()
    .toLowerCase();
}

/** job 的通道。老数据没有 lane 字段，按大批量处理（与 `jobs.lane_of` 同口径）。 */
export function laneOf(job: { lane?: string | null }): 'small' | 'big' {
  return String(job?.lane ?? '') === 'small' ? 'small' : 'big';
}

/**
 * 历史页 / 整批下载的通道口径：按来源，不按调度通道。
 *
 * 批次生产的零头、失败重试常常只有 1–3 条，`jobs.create()` 会把它们判成 lane=small，
 * 但它们属于某个真实批次：历史里必须跟批次一起出现在「全部 / 大批量」，整批下载也要
 * 收得到。只有没有 batch_ref 的才是真正的小批量试跑。
 */
export function historyLane(job: { lane?: string | null; batch_ref?: string | null }): 'small' | 'big' {
  if (String(job?.batch_ref ?? '')) return 'big';
  return laneOf(job);
}

/** 按服务器本地时区把时间戳归到 YYYY-MM-DD；缺时间戳归 unknown，绝不丢记录。 */
export function dayKey(ts: unknown): string {
  const number = Number(ts ?? 0);
  const value = Number.isFinite(number) ? number : 0;
  if (!(value > 0)) return 'unknown';
  const date = new Date(value * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 密钥脱敏（与 `jobs._redact` 同一套模式）。 */
export function redact(text: unknown): string {
  let result = String(text ?? '');
  result = result.replace(/sk-[A-Za-z0-9._-]{12,}/g, '[已隐藏密钥]');
  result = result.replace(/(q-signature|q-ak|SecretId|SecretKey)=([^&\s]+)/gi, '$1=[已隐藏]');
  return result;
}

function stripControl(text: unknown): string {
  // Python: re.sub(r"[\x00-\x1f\x7f]", "", ...)
  // eslint-disable-next-line no-control-regex
  return String(text ?? '').replace(/[\x00-\x1f\x7f]/g, '');
}

/** 单个标签值：去控制字符与首尾空白；款号类只认英文数字 -_，场景/备注放行中文。 */
function cleanTagValue(slot: string, value: unknown): string {
  const text = stripControl(value).trim();
  if (!text) return '';
  if (slot === 'note') return text.slice(0, NOTE_MAX);
  if (slot === 'scene') return text.slice(0, STYLE_MAX);
  if (!TAG_CODE_RE.test(text)) {
    const label = TAG_LABELS[slot] ?? slot;
    throw new BatchError(`「${label}」款号只能用英文、数字、- 和 _，最多 40 个字符`);
  }
  return text;
}

/** 整理 6 栏位标签 dict：空值丢弃；top（上衣）缺失直接拒绝（路径段靠它）。 */
export function cleanTags(raw: unknown): Record<string, string> {
  const source = isRecord(raw) ? raw : {};
  const tags: Record<string, string> = {};
  for (const [slot] of TAG_SLOTS) {
    const value = cleanTagValue(slot, source[slot]);
    if (value) tags[slot] = value;
  }
  if (!tags.top) throw new BatchError('请填写「上衣」款号（云端归档路径按它命名）');
  return tags;
}

/** 旧款号字段（v0.9.0 及以前）：仅用于读取老数据。 */
export function cleanStyleNo(value: unknown): string {
  return stripControl(value).trim().slice(0, STYLE_MAX);
}

/** 时长校验，与 `app/wan_api.py::validate_task` 同口径：-1 跟随素材，或 2–30 秒。 */
export function durationOk(value: unknown): boolean {
  const number = asInt(value);
  if (number === null) return false;
  return number === -1 || (number >= 2 && number <= 30);
}

/**
 * 统一读图：`images[]` 是唯一真源（下标=槽位=imageUrls 顺序）；老 job / 老 batch
 * 只有单值 image_path/image_name，回退成 1 个元素，upload_id 从路径反解。
 * 传 dataDir 时把持久化的绝对路径按当前数据根重锚定（见 `common/paths`）。
 */
export function imageSlots(
  record: { images?: unknown; image_path?: unknown; image_name?: unknown },
  dataDir?: string,
): BatchImageSlot[] {
  const fix = (raw: unknown): string => {
    const text = String(raw ?? '');
    if (!text || !dataDir) return text;
    return relocate(text, dataDir);
  };
  const raw = record?.images;
  if (Array.isArray(raw) && raw.length) {
    const slots: BatchImageSlot[] = [];
    for (const item of raw) {
      if (!isRecord(item)) continue;
      slots.push({
        upload_id: String(item.upload_id ?? ''),
        name: String(item.name ?? ''),
        path: fix(item.path),
      });
    }
    if (slots.length) return slots;
  }
  const single = String(record?.image_path ?? '');
  if (!single) return [];
  const hit = UPLOAD_PATH_RE.exec(single);
  return [
    {
      upload_id: hit ? String(hit[1]) : '',
      name: String(record?.image_name ?? ''),
      path: fix(single),
    },
  ];
}

/** 读 job 记录上 Python 有、TS 类型还没声明的字段（如 archive_error）。 */
function rawField(record: object, key: string): unknown {
  return (record as unknown as Record<string, unknown>)[key];
}

/**
 * 只做「排队」的互斥：Node 单线程，临界区本身是同步的，这里只保证两个异步方法
 * 不会在 `await` 边界处交错进入。**不可重入**——私有方法一律不取锁。
 */
class AsyncMutex {
  private tail: Promise<void> = Promise.resolve();

  async run<T>(fn: () => T | Promise<T>): Promise<T> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await fn();
    } finally {
      release();
    }
  }
}

interface InternalGroup {
  key: string;
  job_count: number;
  total: number;
  success: number;
  failed: number;
  unfinished: number;
  first_at: number | null;
  last_at: number | null;
  owners: Set<string>;
  sig: string[];
  small_jobs: number;
  big_jobs: number;
}

// ---------------------------------------------------------------- 服务

@Injectable()
export class BatchManagerService implements OnApplicationBootstrap {
  private readonly logger = new Logger(BatchManagerService.name);

  /** 批次与任务共用同一把临界区（Python 复用 `jobs.lock`）；见文件头的差异说明。 */
  private readonly mutex = new AsyncMutex();
  private readonly batches = new Map<string, Batch>();
  private readonly byNo = new Map<string, string>();
  private readonly root: string;
  private loaded = false;

  /** 非致命告警（批次号重复、款号记账失败…），只在日志里体现。 */
  readonly warnings: string[] = [];

  /** 款号-模板记账本：批次生产提交时记账，供前端「同款号不重复用模板」标记。 */
  readonly usage: StyleUsageLedger;

  constructor(
    private readonly config: AppConfigService,
    private readonly jobs: JobManagerService,
    private readonly catalog: TemplateCatalogService,
    private readonly uploads: UploadsService,
    private readonly prompts: PromptsService,
  ) {
    this.root = path.join(this.config.dataDir, 'batches');
    fs.mkdirSync(this.root, { recursive: true });
    this.usage = new StyleUsageLedger(this.config.dataDir);
  }

  /**
   * 载入放到 `onApplicationBootstrap`：此时所有模块的 `onModuleInit` 都跑完了，
   * `JobManagerService` 的 jobs Map 一定是满的，`_reconcile` 不会把 producing 误判成 pending。
   */
  onApplicationBootstrap(): void {
    this.load();
  }

  // ---------------------------------------------------------------- 存储

  private batchPath(batchId: string): string {
    return path.join(this.root, `${batchId}.json`);
  }

  private save(batch: Batch): void {
    const file = this.batchPath(batch.id);
    // Python 是 `path.with_suffix(".tmp")`：batches/<id>.tmp
    const tmp = file.replace(/\.json$/, '.tmp');
    fs.writeFileSync(tmp, JSON.stringify(batch, null, 2), 'utf8');
    fs.renameSync(tmp, file);
  }

  /** 尽力而为地落盘：用于已经无法回头（job 已建）的收尾路径。 */
  private saveQuiet(batch: Batch): void {
    try {
      this.save(batch);
    } catch {
      /* 尽力而为 */
    }
  }

  /** 启动时建索引；重复批次号保留首个并告警，坏文件跳过不阻断启动。幂等。 */
  load(): void {
    if (this.loaded) return;
    let names: string[] = [];
    try {
      names = fs
        .readdirSync(this.root)
        .filter((name) => name.endsWith('.json'))
        .sort();
    } catch (err) {
      this.logger.warn(`批次目录不可读：${(err as Error).message}`);
      return;
    }
    for (const name of names) {
      const file = path.join(this.root, name);
      let parsed: unknown;
      try {
        parsed = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
      } catch {
        continue; // 坏文件跳过，不阻断启动
      }
      if (!isRecord(parsed)) continue;
      const batch = parsed as unknown as Batch;
      const batchId = String(batch.id || path.basename(name, '.json')).trim();
      if (!batchId) continue;
      batch.id = batchId;
      if (!(BATCH_STATES as readonly string[]).includes(String(batch.state))) batch.state = 'draft';
      if (!Array.isArray(batch.items)) batch.items = [];
      if (!Array.isArray(batch.runs)) batch.runs = [];
      for (const item of batch.items) {
        if (!isRecord(item)) continue;
        if (!(ITEM_STATES as readonly string[]).includes(String(item.state))) item.state = 'pending';
        if (item.job_id === undefined || item.job_id === null) item.job_id = '';
        if (item.error === undefined || item.error === null) item.error = '';
      }
      const key = normNo(batch.batch_no);
      if (key && this.byNo.has(key)) {
        this.warnings.push(`批次号重复，保留首个：${batch.batch_no}`);
        this.logger.warn(`批次号重复，保留首个：${batch.batch_no}`);
      } else if (key) {
        this.byNo.set(key, batchId);
      }
      this.batches.set(batchId, batch);
      this.refresh(batch);
      // 重启后首次读取就收敛：producing 条目按 records 落 done/failed/回 pending。
      this.reconcile(batch);
    }
    this.loaded = true;
    this.logger.log(`已载入 ${this.batches.size} 个批次`);
  }

  // ---------------------------------------------------------------- 内部工具

  /**
   * 款号-模板账本的键：优先 6 栏位里的「上衣」，老批次回退 style_no。
   * 与 `_startRun` 记账时同一口径——记账和查询必须用同一把键，否则「已用」永远查不到。
   */
  private usageStyle(batch: Batch): string {
    const tags = isRecord(batch.tags) ? batch.tags : {};
    return String(tags.top ?? '').trim() || cleanStyleNo(batch.style_no);
  }

  private requireCatalog(): TemplateCatalogService {
    if (!this.catalog.items.length) {
      throw new BatchError('视频场景暂不可用，请联系管理员。', 503);
    }
    return this.catalog;
  }

  /** 候选集可能还没构建过（只有 bootstrap 会主动建）；空的时候补一次，build() 自身有缓存。 */
  private async ensureCatalog(): Promise<void> {
    if (this.catalog.items.length) return;
    await this.catalog.build();
  }

  /** 取模板当前状态（可能已下架）；不存在返回 null。 */
  private live(templateId: unknown): TemplateRow | null {
    try {
      return this.catalog.resolve(String(templateId ?? ''));
    } catch (err) {
      if (err instanceof TemplateError) return null;
      throw err;
    }
  }

  /** Python `_template_payload`：交给 jobs.create 的快照字段。 */
  private templatePayload(item: TemplateRow): Job['templates'][number] {
    return {
      id: String(item.id),
      name: String(item.name ?? ''),
      url: String(item.url ?? ''),
      key: String(item.key ?? ''),
      duration: item.duration,
    };
  }

  private refresh(batch: Batch): void {
    const items = batch.items ?? [];
    batch.total_items = items.length;
    batch.done = items.filter((item) => item.state === 'done').length;
    batch.failed = items.filter((item) => item.state === 'failed').length;
    batch.pending = items.filter((item) => item.state === 'pending').length;
    batch.producing = items.filter((item) => item.state === 'producing').length;
  }

  private authorized(batchId: string, user: AuthedUser): Batch {
    const batch = this.batches.get(String(batchId ?? ''));
    if (!batch) throw new BatchError('找不到该批次。', 404);
    if (user?.role !== 'admin' && batch.owner !== user?.username) {
      throw new BatchError('无权操作该批次。', 403);
    }
    return batch;
  }

  async get(batchId: string): Promise<Batch | undefined> {
    return this.mutex.run(() => this.batches.get(String(batchId ?? '')));
  }

  /** 所有 job（`list('', true, '')` 是 JobManagerService 的「无筛选」口径，即管理员 scope=all）。 */
  private allJobs(): Job[] {
    return this.jobs.list('', true, '');
  }

  /**
   * 可见 job：普通账号只见自己的；管理员 all_users 时按 owner 过滤。
   * `JobManagerService.list()` 对管理员不做「只看我的」过滤，所以这里再收敛一次。
   */
  private visibleJobs(user: AuthedUser, allUsers: boolean, filterOwner: string): Job[] {
    const rows = this.jobs.list(user?.username ?? '', Boolean(allUsers), filterOwner);
    return rows.filter((job) =>
      allUsers ? !filterOwner || job.owner === filterOwner : job.owner === user?.username,
    );
  }

  // ---------------------------------------------------------------- 对账

  /**
   * 把「已判失败、但对应 job 记录后来变 success」的条目改回 done。
   *
   * 典型场景：等待超时被收成 failed，之后点「复查超时任务」按原 taskId 续查到
   * 云端已出片，job 记录翻成 success——批次页必须跟着变成已完成，否则用户会
   * 对同一条模板再点一次「重试失败」，那才是真正的二次计费。
   *
   * 只做 failed → done 的单向升级，绝不把 done 降回去（续查只会让结果变好）。
   */
  private healItems(batch: Batch): boolean {
    let changed = false;
    for (const item of batch.items ?? []) {
      if (item.state !== 'failed') continue;
      const jobId = String(item.job_id || '');
      if (!jobId) continue;
      const job = this.jobs.get(jobId);
      if (!job) continue;
      for (const rec of job.records ?? []) {
        if (String(rec.id) === String(item.template_id) && String(rec.status) === 'success') {
          item.state = 'done';
          item.error = '';
          delete item.download_failed;
          changed = true;
          break;
        }
      }
    }
    if (changed) this.refresh(batch);
    return changed;
  }

  /**
   * 把 active_job_id 对应 job 的结果收敛回条目状态；有改动返回 true。
   *
   * 必须在临界区内调用（内部只读 job 快照，不再取 jobs 侧的锁，避免互相等待）。
   */
  private reconcile(batch: Batch): boolean {
    const healed = this.healItems(batch);
    let jobId = String(batch.active_job_id || '');
    if (!jobId) {
      // 上一次生产建了 job 但批次没来得及标 producing（写盘失败 / 进程被杀）：
      // 先认领，别让用户再点一次生产导致同批模板被重复提交。
      jobId = this.adoptPendingRun(batch);
      if (!jobId) {
        if (healed) {
          batch.updated_at = nowSeconds();
          this.save(batch);
        }
        return healed;
      }
    }
    const job = this.jobs.get(jobId);
    if (job && !TERMINAL_JOB.has(job.status)) return false;
    const records = new Map<string, JobRecord>();
    if (job) {
      for (const rec of job.records ?? []) {
        if (isRecord(rec)) records.set(String(rec.id), rec);
      }
    }
    let success = 0;
    let failed = 0;
    for (const item of batch.items ?? []) {
      if (item.state !== 'producing') continue;
      if (String(item.job_id || '') !== jobId) continue;
      const rec = records.get(String(item.template_id));
      if (!rec) {
        // job 里查不到这条：多半是建 job 后立刻崩了，回待生产最安全。
        item.state = 'pending';
        item.job_id = '';
        item.error = '';
        continue;
      }
      const status = String(rec.status ?? '');
      if (RECORD_DONE.has(status)) {
        item.state = 'done';
        item.error = '';
        delete item.download_failed;
        success += 1;
      } else if (RECORD_UNTOUCHED.has(status)) {
        item.state = 'pending';
        item.job_id = '';
        item.error = '';
        delete item.download_failed;
      } else {
        item.state = 'failed';
        item.error =
          redact(String(rec.error ?? '')).slice(0, 300) || `任务状态 ${status || '未知'}`;
        // 云端已出片、只是本机下载失败：标出来，重试时必须跳过（重试=二次提交=二次计费），
        // 只能走「复查补下载」。
        if (rec.download_failed) item.download_failed = true;
        else delete item.download_failed;
        failed += 1;
      }
    }
    batch.active_job_id = '';
    const finished = nowSeconds();
    for (const run of [...(batch.runs ?? [])].reverse()) {
      if (String(run.job_id) === jobId && !run.finished_at) {
        run.finished_at = finished;
        run.success = success;
        const blocked = Number(run.blocked ?? 0) || 0;
        // Python: run["failed"] = failed + int(run.pop("blocked", 0) or 0)
        delete run.blocked;
        run.failed = failed + blocked;
        break;
      }
    }
    batch.updated_at = finished;
    this.refresh(batch);
    this.save(batch);
    return true;
  }

  /**
   * 认领 `pending_run` 标记，返回可对账的 job id（没有则空串）。
   *
   * `_startRun` 在 `jobs.create` **之前**就把标记落盘，建完立刻把 job_id 写回，
   * 所以进程在中途被杀 / 写盘失败时，磁盘上一定留有线索：
   *   · 没有 job_id → 任务根本没建，条目保持 pending，清掉标记即可；
   *   · 有 job_id 但 job 查不到 → 云端状态无法确认，标失败交给人工核对，不自动重发；
   *   · 有 job_id 且 job 在 → 接回 producing，让正常对账继续收敛。
   */
  private adoptPendingRun(batch: Batch): string {
    const pending: unknown = batch.pending_run;
    if (!isRecord(pending)) return '';
    const jobId = String(pending.job_id || '');
    const templateIds = new Set(
      (Array.isArray(pending.template_ids) ? pending.template_ids : []).map((tid) => String(tid)),
    );
    const job = jobId ? this.jobs.get(jobId) : undefined;
    if (!jobId) {
      delete batch.pending_run;
    } else if (!job) {
      for (const item of batch.items ?? []) {
        if (templateIds.has(String(item.template_id)) && item.state === 'pending') {
          item.state = 'failed';
          item.error = '上次生产中断：对应任务已找不到，请核对后重试。';
        }
      }
      delete batch.pending_run;
    } else {
      for (const item of batch.items ?? []) {
        if (templateIds.has(String(item.template_id)) && item.state === 'pending') {
          item.state = 'producing';
          item.job_id = jobId;
          item.error = '';
        }
      }
      batch.active_job_id = jobId;
      if (!Array.isArray(batch.runs)) batch.runs = [];
      if (!batch.runs.some((run) => String(run.job_id) === jobId)) {
        batch.runs.push({
          job_id: jobId,
          count: templateIds.size,
          started_at: Number(pending.started_at) || nowSeconds(),
          finished_at: null,
          success: 0,
          failed: 0,
          blocked: 0,
        });
      }
      delete batch.pending_run;
    }
    batch.updated_at = nowSeconds();
    this.refresh(batch);
    this.save(batch);
    return job ? jobId : '';
  }

  // ---------------------------------------------------------------- 估算金额

  /**
   * 金额与单价只对管理员下发，普通用户剥离（可反推 wan.price_per_second）。
   * options 的 count 档位保留：前端生产按钮的档位靠它，只剥金额。
   */
  private moneyView(user: AuthedUser, estimate: CostEstimate): Record<string, unknown> {
    if (user?.role === 'admin') return { ...estimate } as unknown as Record<string, unknown>;
    const view: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(estimate)) {
      if (key === 'unit_price' || key === 'price_source' || key === 'amount') continue;
      view[key] = value;
    }
    if (Array.isArray(view.options)) {
      view.options = (view.options as Array<Record<string, unknown>>).map((option) => {
        const copy: Record<string, unknown> = {};
        for (const [key, value] of Object.entries(option)) if (key !== 'amount') copy[key] = value;
        return copy;
      });
    }
    return view;
  }

  /**
   * 按（模板时长 + 输出秒数）× 单价预估；单价缺失时 amount 为 null。
   * outputSeconds 缺省 7 秒，与批次冻结的 duration 对齐（duration=-1 也按 7 估算）。
   */
  estimate(
    liveItems: ReadonlyArray<{ duration?: unknown }>,
    resolution: string,
    outputSeconds?: number | null,
  ): CostEstimate {
    const outSeconds =
      outputSeconds && outputSeconds > 0 ? Number(outputSeconds) : OUTPUT_SECONDS;
    const seconds = liveItems.map(
      (item) => (asFloat(item?.duration) ?? DEFAULT_TEMPLATE_SECONDS) + outSeconds,
    );
    const total = pyRound(seconds.reduce((sum, value) => sum + value, 0), 2);
    const { price: unit, source } = this.config.unitPrice(resolution);
    const count = seconds.length;
    const perItem = count ? pyRound(total / count, 3) : DEFAULT_TEMPLATE_SECONDS + outSeconds;
    const options: Array<{ count: number; amount: number | null }> = [];
    for (const choice of PRODUCE_CHOICES) {
      if (choice > this.config.maxBatchRunItems) continue;
      options.push({
        count: choice,
        amount: unit ? pyRound(choice * perItem * unit, 2) : null,
      });
    }
    return {
      count,
      seconds: total,
      unit_price: unit,
      price_source: source,
      amount: unit ? pyRound(total * unit, 2) : null,
      resolution,
      options,
      currency: 'CNY',
      note: `按（模板时长 + 输出 ${formatG(outSeconds)} 秒）× 单价预估，实际以账单为准`,
    };
  }

  /** 批次冻结的输出秒数：duration>0 用它，-1（跟随素材）按 7 秒估算。 */
  outputSecondsOf(batch: Batch): number {
    const duration = asInt(batch.duration);
    return duration && duration > 0 ? Number(duration) : OUTPUT_SECONDS;
  }

  // ---------------------------------------------------------------- 多图槽位

  /** 统一读图：images[] 为真源；老批次回退成单元素列表。顺序 = 槽位 = 模型「图N」。 */
  private imageSlotsOf(batch: Batch): BatchImageSlot[] {
    return imageSlots(batch, this.config.dataDir);
  }

  /**
   * images[0] 回写单值别名，让老代码/老前端/历史对比层继续只认主图。
   * 别名同样按当前数据根重锚，避免把失效的旧绝对路径又写回批次 JSON。
   */
  private syncImageAliases(batch: Batch): void {
    const slots = this.imageSlotsOf(batch);
    const first = slots[0];
    batch.image_upload_id = String(first?.upload_id ?? '');
    batch.image_path = String(first?.path ?? '');
    batch.image_name = String(first?.name ?? '');
  }

  /** 把 upload_ids（回退 upload_id）解析成有序槽位；逐张校验归属与文件存在。 */
  private parseUploads(user: AuthedUser, data: Record<string, unknown>, allowEmpty: boolean): BatchImageSlot[] {
    const raw = data?.upload_ids ?? undefined;
    let list: unknown;
    if (raw === undefined || raw === null) {
      const single = String(data?.upload_id ?? '').trim();
      list = single ? [single] : [];
    } else {
      list = raw;
    }
    if (!Array.isArray(list)) throw new BatchError('图片参数无效，请重新上传。');
    const ids = list.map((item) => String(item ?? '').trim()).filter((item) => item.length > 0);
    if (!ids.length) {
      if (allowEmpty) return [];
      throw new BatchError('请先上传定妆图。');
    }
    if (ids.length > MAX_IMAGE_SLOTS) {
      throw new BatchError(
        `一次最多 ${MAX_IMAGE_SLOTS} 张图片（1 张定妆图 + 9 张细节图），当前 ${ids.length} 张。`,
      );
    }
    if (new Set(ids).size !== ids.length) throw new BatchError('同一张图片不能重复提交。');
    const slots: BatchImageSlot[] = [];
    ids.forEach((uploadId, index) => {
      if (!/^[a-f0-9]{32}$/.test(uploadId)) {
        throw new BatchError(`第 ${index + 1} 张图片已失效，请重新上传。`, 404);
      }
      const meta = this.uploads.resolve(uploadId, user?.username ?? '');
      if (!meta) throw new BatchError(`第 ${index + 1} 张图片已失效，请重新上传。`, 404);
      slots.push({
        upload_id: uploadId,
        name: String(meta.meta.name || ''),
        path: meta.file,
      });
    });
    return slots;
  }

  /**
   * 这个批次名下「还有 timeout / 下载失败记录」的 job id（runs 为主，items 兜底）。
   * 调用方需在临界区内（只读内存，不再取 jobs 侧锁）。
   */
  private timeoutJobs(batch: Batch): string[] {
    const seen = new Set<string>();
    const ordered: string[] = [];
    for (const run of batch.runs ?? []) {
      const jobId = String(run.job_id || '');
      if (jobId && !seen.has(jobId)) {
        seen.add(jobId);
        ordered.push(jobId);
      }
    }
    for (const item of batch.items ?? []) {
      const jobId = String(item.job_id || '');
      if (jobId && !seen.has(jobId)) {
        seen.add(jobId);
        ordered.push(jobId);
      }
    }
    const keep: string[] = [];
    for (const jobId of ordered) {
      const job = this.jobs.get(jobId);
      if (!job) continue;
      if ((job.records ?? []).some((rec) => needsRecheck(rec))) keep.push(jobId);
    }
    return keep;
  }

  /**
   * 对这个批次里所有含超时记录的 job 逐个发起「复查」（只查云端，不重新提交）。
   *
   * 为什么放在批次上：一次大批量跑完，超时条目通常散在同一个 job 的十几条记录里，
   * 用户真正想点的是「把这批超时的都查一遍」，而不是逐个 job 点。
   */
  async recheckTimeouts(
    batchId: string,
    user: AuthedUser,
  ): Promise<{ jobs: string[]; skipped: Array<{ job_id: string; reason: string }>; total_jobs: number }> {
    const targets = await this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      return this.timeoutJobs(batch);
    });
    if (!targets.length) throw new BatchError('这个批次没有等待超时的条目。', 409);
    const started: string[] = [];
    const skipped: Array<{ job_id: string; reason: string }> = [];
    for (const jobId of targets) {
      try {
        // 注意：这里**不持 mutex** 调用 jobs 侧，避免两把锁交叉持有。
        this.jobs.recheckTimeouts(jobId, true);
        started.push(jobId);
      } catch (err) {
        skipped.push({ job_id: jobId, reason: (err as Error).message });
      }
    }
    return { jobs: started, skipped, total_jobs: targets.length };
  }

  // ---------------------------------------------------------------- 公开字段

  publicView(batch: Batch): BatchView {
    const source = batch as unknown as Record<string, unknown>;
    const data: Record<string, unknown> = {};
    for (const key of PUBLIC_KEYS) data[key] = source[key];
    // image_url 语义固定为「主图（定妆图）」，老前端/历史对比层靠它，不能改。
    data.image_url = data.image_upload_id ? `/api/uploads/${String(data.image_upload_id)}` : '';
    const slots = this.imageSlotsOf(batch);
    data.image_urls = slots
      .filter((slot) => slot.upload_id)
      .map((slot) => `/api/uploads/${slot.upload_id}`);
    data.image_names = slots.map((slot) => slot.name);
    data.runs = (batch.runs ?? []).map((run) => ({ ...run }));
    // 还有超时/下载失败记录的 job：前端据此显示「复查补下载」按钮，
    // 复查成功时批次条目会被 healItems 自动改回已完成。
    data.timeout_jobs = this.timeoutJobs(batch);
    // 云端已出片、只是下载失败的条目数：重试会跳过它们，前端要把话说清楚。
    data.download_failed_items = (batch.items ?? []).filter((item) => item.download_failed).length;
    return data;
  }

  /** 按 job 缓存 records 索引，避免 500 条条目反复遍历同一个 job。 */
  private recordIndex(cache: Map<string, Map<string, JobRecord>>, jobId: string): Map<string, JobRecord> {
    if (!jobId) return new Map();
    let index = cache.get(jobId);
    if (index) return index;
    const job = this.jobs.get(jobId);
    index = new Map();
    for (const rec of job?.records ?? []) {
      if (isRecord(rec)) index.set(String(rec.id), rec);
    }
    cache.set(jobId, index);
    return index;
  }

  /**
   * 条目公开字段 + download_url + verify_warn。
   *
   * - download_url：done 且有 job 时给，路径与历史分组同源；
   * - verify_warn：模型实际输出偏离预期（时长不合/文件过小）时的质量提示，
   *   状态仍是 done，前端当提示用、不要当失败。
   */
  publicItems(batch: Batch, itemState = ''): BatchItemView[] {
    let items: BatchItemView[] = [];
    const cache = new Map<string, Map<string, JobRecord>>();
    for (const item of batch.items ?? []) {
      const row: BatchItemView = {
        ...item,
        tags: [...(item.tags ?? [])],
        download_url: '',
        verify_warn: '',
      };
      const jobId = String(item.job_id || '');
      const templateId = String(item.template_id || '');
      const done = item.state === 'done';
      row.download_url =
        done && jobId && templateId ? `/api/jobs/${jobId}/download/${templateId}` : '';
      let warn = '';
      if (done && jobId) {
        const rec = this.recordIndex(cache, jobId).get(templateId);
        const verify = rec?.verify;
        warn = isRecord(verify) ? String(verify.warn ?? '') : '';
      }
      row.verify_warn = redact(warn).slice(0, 200);
      items.push(row);
    }
    if (itemState) items = items.filter((item) => item.state === itemState);
    return items;
  }

  /**
   * 按「待生产条目」算预估金额，供批次详情复用（重开已锁定批次时前端不必再拉候选）。
   * 模板时长优先取当前清单里的实时值；模板已下架则退回默认时长，绝不因此报错。
   */
  estimateForPending(batch: Batch): CostEstimate {
    const pending = (batch.items ?? []).filter((item) => item.state === 'pending');
    const liveItems: Array<TemplateRow | Record<string, never>> = [];
    const catalog = this.catalog.items.length ? this.catalog : null;
    for (const item of pending) {
      liveItems.push((catalog ? this.live(item.template_id) : null) ?? {});
    }
    return this.estimate(liveItems, String(batch.resolution || '720P'), this.outputSecondsOf(batch));
  }

  // ---------------------------------------------------------------- #1 创建

  async create(user: AuthedUser, data: Record<string, unknown>): Promise<BatchView> {
    const batchNo = String(data?.batch_no ?? '').trim();
    const title = String(data?.title ?? '').trim();
    const promptId = String(data?.prompt_id ?? '').trim();
    const prompt = String(data?.prompt ?? '').trim();
    // 与 Python 一致：**不设默认值**，缺省即空串 → 下面的「只能是 480P 或 720P」直接拒绝。
    const resolution = String(data?.resolution ?? '')
      .trim()
      .toUpperCase();
    // 网页标配「输出固定 7 秒」；-1（跟随素材）与 2–30 的其他秒数仍接受，向后兼容老批次。
    const duration = asIntOr(data?.duration, DEFAULT_DURATION);
    // 多图：upload_ids 有序（槽位 1 = 定妆图）；缺省时回退老的单个 upload_id。
    // 草稿态允许 0 张（后补），所以 allowEmpty=true。
    const slots = this.parseUploads(user, data ?? {}, true);

    if (!batchNo || batchNo.length > 40) throw new BatchError('批次号需要 1–40 个字符。');
    if (/[\r\n\t]/.test(batchNo)) throw new BatchError('批次号不能包含换行或制表符。');
    if (title.length > 60) throw new BatchError('备注最多 60 个字符。');
    const promptIds = new Set(this.prompts.list().map((item) => String(item.id)));
    if (!promptIds.has(promptId)) throw new BatchError('请选择有效的提示词模板。');
    if (!prompt) throw new BatchError('提示词不能为空。');
    if (prompt.length > 4000) throw new BatchError('提示词最多 4000 个字符。');
    if (resolution !== '480P' && resolution !== '720P') {
      throw new BatchError('画质只能是 480P 或 720P。');
    }
    if (!durationOk(duration)) throw new BatchError('时长只能跟随素材（-1）或 2–30 秒。');
    // 6 栏位标签：上衣必填（进 COS 归档路径），其余选填。旧批次只有 style_no，不强制补填。
    const batchTags = cleanTags(data?.tags);

    return this.mutex.run(() => {
      if (this.byNo.has(normNo(batchNo))) throw new BatchError('批次号已存在，请换一个。', 409);
      let batchId = crypto.randomBytes(6).toString('hex');
      while (this.batches.has(batchId)) batchId = crypto.randomBytes(6).toString('hex');
      const now = nowSeconds();
      const first = slots[0];
      const batch: Batch = {
        id: batchId,
        batch_no: batchNo,
        title,
        owner: user.username,
        // 标签：6 栏位（上衣必填）；style_no 只保留 v0.9.0 老批次的值。
        style_no: cleanStyleNo(data?.style_no),
        tags: batchTags,
        state: 'draft',
        // images[] 是唯一真源；三个单值字段是主图别名，供老前端/老代码读取。
        images: slots,
        image_upload_id: String(first?.upload_id ?? ''),
        image_path: String(first?.path ?? ''),
        image_name: String(first?.name ?? ''),
        prompt_id: promptId,
        prompt,
        resolution,
        duration,
        tag_filters: {},
        tag_excludes: {},
        items: [],
        runs: [],
        active_job_id: '',
        total_items: 0,
        done: 0,
        failed: 0,
        pending: 0,
        producing: 0,
        created_at: now,
        updated_at: now,
        locked_at: null,
        closed_at: null,
      };
      this.batches.set(batchId, batch);
      this.byNo.set(normNo(batchNo), batchId);
      this.save(batch);
      return this.publicView(batch);
    });
  }

  // ---------------------------------------------------------------- #2 绑定定妆图

  /** 草稿态整组重设（换定妆图/重新选 1+9 张）；锁定后只能走 add/remove_images。 */
  async setImage(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<Record<string, unknown>> {
    const slots = this.parseUploads(user, data ?? {}, false);
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      if (batch.state !== 'draft') throw new BatchError('批次已锁定，换图请新开批次。', 409);
      if ((batch.items ?? []).length || (batch.runs ?? []).length) {
        throw new BatchError('本批次已生产过，换图请新开批次。', 409);
      }
      batch.images = slots;
      this.syncImageAliases(batch);
      batch.updated_at = nowSeconds();
      this.save(batch);
      return { image_name: batch.image_name, batch: this.publicView(batch) };
    });
  }

  /**
   * 锁定后仍可追加细节图：只落到批次，对**之后**的生产生效（已建 job 持自己的副本）。
   *
   * 不加「有任务在跑就 409」：`_startRun` 在同一临界区内一次性读完图并建 job，
   * 追加只可能落在生产前或生产后，不存在读一半被改。
   */
  async addImages(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<Record<string, unknown>> {
    const incoming = this.parseUploads(user, data ?? {}, false);
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      if (batch.state === 'closed') throw new BatchError('批次已关闭，不能再改图。', 409);
      const current = this.imageSlotsOf(batch);
      if (current.length + incoming.length > MAX_IMAGE_SLOTS) {
        throw new BatchError(
          `已绑定 ${current.length} 张，最多 ${MAX_IMAGE_SLOTS} 张；本次只能再加 ` +
            `${Math.max(0, MAX_IMAGE_SLOTS - current.length)} 张。`,
        );
      }
      batch.images = [...current, ...incoming];
      this.syncImageAliases(batch);
      batch.updated_at = nowSeconds();
      this.save(batch);
      return { batch: this.publicView(batch), slot_count: batch.images.length };
    });
  }

  /** 删除细节图（槽位 1 定妆图不可删）；删除后自动左移，保证「图N」不错位。 */
  async removeImages(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<Record<string, unknown>> {
    const rawIndex = data?.index ?? null;
    const uploadId = String(data?.upload_id ?? '').trim();
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      if (batch.state === 'closed') throw new BatchError('批次已关闭，不能再改图。', 409);
      const slots = this.imageSlotsOf(batch);
      let index: number;
      if (rawIndex !== null && rawIndex !== undefined) {
        const parsed = asInt(rawIndex);
        if (parsed === null) throw new BatchError('图片序号无效。');
        index = parsed;
      } else {
        index = slots.findIndex((slot) => slot.upload_id === uploadId) + 1;
      }
      if (index <= 1) throw new BatchError('定妆图不能删除；要换请整批重开。');
      if (!slots.length || index > slots.length) {
        throw new BatchError('要删除的细节图不存在。', 404);
      }
      slots.splice(index - 1, 1);
      batch.images = slots;
      this.syncImageAliases(batch);
      batch.updated_at = nowSeconds();
      this.save(batch);
      return { batch: this.publicView(batch), slot_count: slots.length };
    });
  }

  // ---------------------------------------------------------------- #3 候选清单

  async candidates(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<Record<string, unknown>> {
    const tagFilters = data?.tag_filters || {};
    if (!isRecord(tagFilters)) throw new BatchError('标签筛选条件格式无效。');
    // 排除标签：黑名单，模板命中任一排除标签就不进候选（与包含条件相互独立）。
    const tagExcludes = data?.tag_excludes || {};
    if (!isRecord(tagExcludes)) throw new BatchError('排除标签条件格式无效。');
    const limit = asIntOr(data?.limit, 500) || 500;
    if (limit < 1 || limit > 1000) throw new BatchError('limit 需要 1–1000。');
    // 评分筛选：rating_min=3 表示只要打了 3 星及以上的；0 / 缺省 = 不过滤
    const ratingMin = asIntOr(data?.rating_min, 0) || 0;
    if (ratingMin < 0 || ratingMin > 5) throw new BatchError('rating_min 需要 0–5。');
    // 排序：rating = 评分从高到低（同分按编号升序稳定排列）；缺省保持标签库原顺序
    const sort = String(data?.sort ?? '')
      .trim()
      .toLowerCase();
    if (sort !== '' && sort !== 'rating') {
      throw new BatchError('sort 仅支持空（默认）或 rating（评分高到低）。');
    }
    // 「隐藏已用」：本款号在之前批次里生产过的模板直接不进候选（款号-模板账本）。
    // 放在评分/排序之前筛，used_skipped 报的是「标签命中里有多少条被这个开关筛掉了」。
    const hideUsed = data?.hide_used === true;

    await this.ensureCatalog();
    const scope = await this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      return {
        resolution: String(batch.resolution || '720P'),
        outSeconds: this.outputSecondsOf(batch),
        style: this.usageStyle(batch),
      };
    });
    const catalog = this.requireCatalog();
    const filters = tagFilters as Record<string, string[]>;
    const excludes = tagExcludes as Record<string, string[]>;
    let matched: TemplateRow[];
    try {
      matched = catalog.filterItems(filters, excludes);
    } catch (err) {
      if (err instanceof TemplateError) throw new BatchError(err.message);
      throw err;
    }
    // 排除标签筛掉多少条：只用于说明「候选为什么变少」，量级是内存里几百条，重复算一次不心疼。
    let excludeSkipped = 0;
    if (Object.values(excludes).some((value) => value)) {
      let plain: TemplateRow[];
      try {
        plain = catalog.filterItems(filters);
      } catch (err) {
        if (err instanceof TemplateError) throw new BatchError(err.message);
        throw err;
      }
      excludeSkipped = Math.max(0, plain.length - matched.length);
    }
    let usedSkipped = 0;
    if (hideUsed) {
      const usedIds = new Set(Object.keys(this.usage.used(scope.style)));
      if (usedIds.size) {
        const before = matched.length;
        matched = matched.filter((item) => !usedIds.has(String(item.id)));
        usedSkipped = before - matched.length;
      }
    }
    if (ratingMin > 0) {
      matched = matched.filter((item) => Number(item.rating || 0) >= ratingMin);
    }
    if (sort === 'rating') {
      matched = [...matched].sort(
        (a, b) => Number(b.rating || 0) - Number(a.rating || 0) || String(a.id).localeCompare(String(b.id)),
      );
    }
    const page = matched.slice(0, limit);
    const rows = page.map((item) => ({
      id: String(item.id),
      name: String(item.name || ''),
      tags: [...(item.tags ?? [])],
      ready: Boolean(item.url || item.key),
      rating: Number(item.rating || 0),
    }));
    const readyCount = matched.filter((item) => item.url || item.key).length;
    return {
      items: rows,
      total: rows.length,
      filtered_total: matched.length,
      ready_count: readyCount,
      hide_used: hideUsed,
      used_skipped: usedSkipped,
      exclude_skipped: excludeSkipped,
      cost_estimate: this.moneyView(user, this.estimate(page, scope.resolution, scope.outSeconds)),
    };
  }

  // ---------------------------------------------------------------- #4 锁定清单

  async lock(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<BatchView> {
    const rawIds = data?.template_ids;
    if (!Array.isArray(rawIds) || !rawIds.length) throw new BatchError('锁定清单不能为空。');
    if (data?.confirmed !== true) throw new BatchError('请二次确认后再锁定清单。');
    await this.ensureCatalog();
    const catalog = this.requireCatalog();
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      if (batch.state !== 'draft') throw new BatchError('清单已锁定，不能再改。', 409);
      if (!this.imageSlotsOf(batch).length) {
        throw new BatchError('请先绑定定妆图再锁定清单。', 409);
      }
      if (rawIds.length > this.config.maxBatchItems) {
        throw new BatchError(`清单最多 ${this.config.maxBatchItems} 条。`, 409);
      }
      let templates: TemplateRow[];
      try {
        templates = this.pickByIds(catalog, rawIds.map((value) => String(value)), this.config.maxBatchItems);
      } catch (err) {
        if (err instanceof TemplateError) throw new BatchError(err.message);
        throw err;
      }
      batch.items = templates.map((item) => ({
        template_id: String(item.id),
        name: String(item.name || ''),
        tags: [...(item.tags ?? [])],
        state: 'pending' as ItemState,
        job_id: '',
        error: '',
      }));
      batch.tag_filters = isRecord(data?.tag_filters)
        ? ({ ...data.tag_filters } as Record<string, string[]>)
        : {};
      // 排除标签也进快照：锁定后回看批次时要知道这份清单是按什么黑名单筛出来的。
      batch.tag_excludes = isRecord(data?.tag_excludes)
        ? ({ ...data.tag_excludes } as Record<string, string[]>)
        : {};
      batch.state = 'locked';
      batch.locked_at = nowSeconds();
      batch.updated_at = batch.locked_at;
      this.refresh(batch);
      this.save(batch);
      return this.publicView(batch);
    });
  }

  /**
   * 按 ID 列表取模板（保持入参顺序、自动去重），供批次锁定清单使用。
   *
   * 与 Python `TemplateCatalog.pick_by_ids` 同口径：ID 必须存在、必须已上传云端
   * （有 url 或 key）。`TemplateCatalogService` 没搬这个方法，所以在批次侧实现。
   */
  private pickByIds(catalog: TemplateCatalogService, ids: string[], maxItems: number): TemplateRow[] {
    if (!ids.length) throw new TemplateError('清单不能为空');
    const ordered: string[] = [];
    for (const raw of ids) {
      const templateId = normalizeId(raw);
      if (templateId && !ordered.includes(templateId)) ordered.push(templateId);
    }
    if (ordered.length > maxItems) throw new TemplateError(`清单最多 ${maxItems} 条`);
    const chosen: TemplateRow[] = [];
    const pending: string[] = [];
    for (const templateId of ordered) {
      const item = catalog.items.find((row) => row.id === templateId);
      if (!item) throw new TemplateError(`模板 ${templateId} 不在当前视频场景清单里`);
      if (!item.url && !item.key) pending.push(templateId);
      chosen.push(item);
    }
    if (pending.length) {
      throw new TemplateError(
        `所选视频尚未上传到云端模板库（例如 ${pending[0]}，共 ${pending.length} 条），` +
          '先在管理页上传并重载模板清单后再锁定。',
      );
    }
    return chosen;
  }

  // ---------------------------------------------------------------- 生产（#5 / #9 共用）

  /** 账户已占用的生成额度（条），与 Python `JobManager.videos_used` 同口径。 */
  private videosUsed(owner: string): number {
    let used = 0;
    for (const job of this.allJobs()) {
      if (String(job.owner || '') !== owner) continue;
      let counted = 0;
      for (const rec of job.records ?? []) {
        const status = String(rec.status ?? '');
        if (status === 'success' || status === 'running') counted += 1;
      }
      const status = String(job.status ?? '');
      if (status === 'queued' || status === 'running') {
        counted += Math.max(0, Number(job.total || 0) - counted);
      }
      used += counted;
    }
    return used;
  }

  /**
   * 账户生成数量上限：0/缺省 = 不限。超了直接拒绝，不部分提交。
   * settings.users 是 web.json 里的账户表；videos_used 与小批量同口径。
   */
  private quotaCheck(user: AuthedUser, count: number): void {
    const record = this.config.users[String(user?.username ?? '')] ?? {};
    const raw = Number(record.quota ?? 0);
    const quota = Number.isFinite(raw) ? Math.max(0, Math.trunc(raw)) : 0;
    if (quota <= 0) return;
    const used = this.videosUsed(String(user?.username ?? ''));
    if (used + count > quota) {
      throw new BatchError(
        `生成数量已达上限：该账户已用 ${used} 条 / 上限 ${quota} 条，` +
          `本次 ${count} 条会超出额度；请联系管理员调整。`,
        403,
      );
    }
  }

  /** 在临界区内取数 → 校验模板 → 建 job → 标 producing。调用方必须已持 mutex。 */
  private startRun(
    batch: Batch,
    user: AuthedUser,
    wanted: ReadonlyArray<ItemState>,
    count: number,
    emptyMessage: string,
  ): Record<string, unknown> {
    if (batch.state !== 'locked') throw new BatchError('请先锁定清单再生产。', 409);
    if (batch.active_job_id) throw new BatchError('本批次有任务在跑，请等它结束。', 409);
    const slots = this.imageSlotsOf(batch);
    if (!slots.length) throw new BatchError('定妆图已丢失，请重新上传或新开批次。', 404);
    slots.forEach((slot, index) => {
      if (!fs.existsSync(String(slot.path || '')) || !fs.statSync(String(slot.path)).isFile()) {
        throw new BatchError(`第 ${index + 1} 张批次图片已丢失，请重新上传或新开批次。`, 404);
      }
    });
    const imagePath = String(slots[0].path);
    let queue = (batch.items ?? []).filter((item) => wanted.includes(item.state));
    // 「云端已出片、只是下载失败」的条目绝不能重跑：重跑=重新提交=二次计费。
    // 它们只能走批次页的「复查补下载」。这里排掉并计数，前端提示「已跳过 N 条」。
    const blockedDownload = queue.filter((item) => item.download_failed);
    if (blockedDownload.length) queue = queue.filter((item) => !item.download_failed);
    if (!queue.length) {
      if (blockedDownload.length) {
        throw new BatchError(
          `这 ${blockedDownload.length} 条是「云端已出片、只是下载失败」的条目：` +
            '重试会重新提交、二次计费。请先点「复查补下载」按原 taskId 把成片补回来。',
          409,
        );
      }
      throw new BatchError(emptyMessage, 409);
    }
    // 额度校验放在改任何状态之前：超限就是纯拒绝，条目一个都不动。
    this.quotaCheck(user, Math.min(queue.length, Math.max(1, Math.trunc(count || 0))));
    const catalog = this.requireCatalog();
    const taken: Array<{ item: BatchItem; live: TemplateRow }> = [];
    const blocked: string[] = [];
    for (const item of queue.slice(0, count)) {
      const live = this.live(item.template_id);
      if (!live || !(live.url || live.key)) {
        // 锁定的模板已下架：标失败、本次少发一条，绝不静默替换成别的视频。
        item.state = 'failed';
        item.job_id = '';
        item.error = '模板已不可用';
        delete item.download_failed;
        blocked.push(String(item.template_id));
      } else {
        taken.push({ item, live });
      }
    }
    if (!taken.length) {
      this.refresh(batch);
      batch.updated_at = nowSeconds();
      this.save(batch);
      throw new BatchError('待生产条目的模板都已不可用，请检查模板清单。', 503);
    }
    const resolution = String(batch.resolution || '720P');
    // 批次冻结的输出秒数原样下发（7 / -1 / 2–30）；老批次存了非法值也兜回默认。
    let duration = asIntOr(batch.duration, DEFAULT_DURATION);
    if (!durationOk(duration)) duration = DEFAULT_DURATION;
    // 先落「本次生产已开始」的标记，再建 job。jobs.create 之后任何一步失败（写盘、
    // 记账、标记条目）都不会让批次看起来还能再点一次生产——否则同一批条目会再建
    // 一个 job，云端会真的再出一遍片、再收一次钱。
    const marker: BatchPendingRun = {
      template_ids: taken.map(({ item }) => String(item.template_id)),
      job_id: '',
      started_at: nowSeconds(),
    };
    batch.pending_run = marker;
    batch.updated_at = marker.started_at;
    try {
      this.save(batch);
    } catch (err) {
      delete batch.pending_run;
      throw new BatchError(
        `批次状态写入失败，本次没有创建任务，请稍后重试（${(err as Error).message}）`,
        503,
      );
    }
    let job: Job;
    try {
      job = this.jobs.create({
        owner: user.username,
        slots,
        templates: taken.map(({ live }) => this.templatePayload(live)),
        promptId: String(batch.prompt_id),
        prompt: String(batch.prompt || ''),
        resolution,
        duration,
        tags: { ...(batch.tags ?? {}) },
        styleNo: String(batch.style_no || ''),
        batchRef: String(batch.id),
      });
    } catch (err) {
      delete batch.pending_run;
      this.saveQuiet(batch);
      throw new BatchError((err as Error).message, 409);
    }
    const jobId = String(job.id);
    marker.job_id = jobId;
    try {
      // 任务已建：立刻把 job id 落盘，之后哪怕进程被杀也能在对账里认领。
      this.save(batch);
    } catch (err) {
      throw new BatchError(
        `任务已创建（${jobId}），但批次状态写入失败：${(err as Error).message}；` +
          '请勿重复生产，刷新后查看，或联系管理员核对。',
        503,
      );
    }
    // 款号-模板记账（提交时记，失败重试也覆盖为最近一次）：纯审计，失败不能影响已建好的任务。
    try {
      this.usage.record(
        this.usageStyle(batch),
        taken.map(({ item }) => String(item.template_id)),
        String(batch.batch_no || ''),
        String(batch.id),
      );
    } catch {
      this.warnings.push(`批次 ${batch.id} 的款号记账失败（不影响出片）`);
      this.logger.warn(`批次 ${batch.id} 的款号记账失败（不影响出片）`);
    }
    for (const { item } of taken) {
      item.state = 'producing';
      item.job_id = jobId;
      item.error = '';
    }
    batch.active_job_id = jobId;
    if (!Array.isArray(batch.runs)) batch.runs = [];
    batch.runs.push({
      job_id: jobId,
      count: taken.length,
      started_at: nowSeconds(),
      finished_at: null,
      success: 0,
      failed: 0,
      blocked: blocked.length,
    });
    batch.updated_at = nowSeconds();
    delete batch.pending_run;
    try {
      this.refresh(batch);
      this.save(batch);
    } catch (err) {
      batch.pending_run = marker; // 保住内存里的认领凭据，刷新即对账
      throw new BatchError(
        `任务已创建（${jobId}），但批次状态写入失败：${(err as Error).message}；` +
          '请刷新后查看，不要重复生产。',
        503,
      );
    }
    return {
      job: this.jobs.publicView(job, user?.role === 'admin'),
      batch: this.publicView(batch),
      skipped_download_failed: blockedDownload.length,
      taken: taken.length,
      blocked,
      cost_estimate: this.moneyView(
        user,
        this.estimate(
          taken.map(({ live }) => live),
          resolution,
        ),
      ),
    };
  }

  async produce(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<Record<string, unknown>> {
    const count = asInt(data?.count ?? DEFAULT_PRODUCE_COUNT);
    const maxRun = this.config.maxBatchRunItems;
    if (count === null || count <= 0) throw new BatchError('每次生产条数需要是正整数。');
    if (count > maxRun) throw new BatchError(`单次最多生产 ${maxRun} 条。`);
    await this.ensureCatalog();
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      this.reconcile(batch);
      if (count < PRODUCE_MIN) {
        // 低于最小生成单位只有一种合法情形：清单本身只剩这么多（跑掉零头）。
        // 否则 3 条也能随时插队，最小单位就形同虚设。
        const pending = (batch.items ?? []).filter((item) => String(item.state || '') === 'pending').length;
        if (pending && count !== pending) {
          throw new BatchError(
            `每次最少生产 ${PRODUCE_MIN} 条；清单只剩 ${pending} 条时可以一次提交全部剩余。`,
          );
        }
      }
      return this.startRun(
        batch,
        user,
        ['pending'],
        count,
        '没有待生产的条目，可重试失败项或关闭批次。',
      );
    });
  }

  async retryFailed(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<Record<string, unknown>> {
    const count = asInt(data?.count ?? RETRY_MAX);
    if (count === null || count < RETRY_MIN || count > RETRY_MAX) {
      throw new BatchError(`重试条数需要 ${RETRY_MIN}–${RETRY_MAX}。`);
    }
    await this.ensureCatalog();
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      this.reconcile(batch);
      return this.startRun(batch, user, ['failed'], count, '没有失败的条目需要重试。');
    });
  }

  // ---------------------------------------------------------------- #8 关闭

  async close(batchId: string, user: AuthedUser): Promise<BatchView> {
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      this.reconcile(batch);
      if (batch.active_job_id) throw new BatchError('还有任务在跑，请等结束后再关闭批次。', 409);
      if (batch.state === 'closed') return this.publicView(batch);
      batch.state = 'closed';
      batch.closed_at = nowSeconds();
      batch.updated_at = batch.closed_at;
      this.save(batch);
      return this.publicView(batch);
    });
  }

  // ---------------------------------------------------------------- #14 修改提示词

  /**
   * 只改批次的提示词正文，其余（清单/定妆图/画质/时长）仍冻结。
   *
   * 为什么锁定后允许改：`startRun` 是在**每次生产时**才读 `batch.prompt` 建任务，
   * 所以改提示词不影响已生成的视频，只影响之后的分批生产。
   * 运行中也允许改：`startRun` 在提交那一刻（同样在临界区内）就把 prompt 快照进了 job，
   * 之后改批次上的 prompt 只对**下一轮**生效。只有批次已关闭才拒绝。
   */
  async updatePrompt(batchId: string, user: AuthedUser, data: Record<string, unknown>): Promise<BatchView> {
    const prompt = String(data?.prompt ?? '').trim();
    if (!prompt) throw new BatchError('提示词不能为空。');
    if (prompt.length > 4000) throw new BatchError('提示词最多 4000 个字符。');
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      this.reconcile(batch);
      if (batch.state === 'closed') throw new BatchError('批次已关闭，提示词不可再修改。', 409);
      batch.prompt = prompt;
      batch.updated_at = nowSeconds();
      this.save(batch);
      return this.publicView(batch);
    });
  }

  // ---------------------------------------------------------------- #12 删除

  /**
   * 删除批次：只删 web-data/batches/<id>.json 这一个文件。
   *
   * 安全口径：
   *   · 权限：owner 或 admin；不存在 404、非 owner 403（与详情同一套 authorized）。
   *   · 运行中禁删：`active_job_id` 非空（job 未终态）→ 409。
   *   · **不碰任何产物**：output/、jobs/ 下的文件不删不移动——已生成的视频属于 job，
   *     删批次不等于删视频；job 的 batch_ref 也保持原值，历史分组按「批次文件是否还在」
   *     判断归属，删掉后这些 job 自然落回 __ungrouped__。
   */
  async delete(batchId: string, user: AuthedUser): Promise<{ id: string }> {
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      this.reconcile(batch); // 先对账：job 已终态则清空 active_job_id，才允许删
      if (batch.active_job_id) {
        throw new BatchError('批次正在生产中，请等待生产结束或取消任务后再删除', 409);
      }
      const target = batch.id;
      try {
        // 先删文件再摘索引：落盘失败就整体失败（500），不留「内存没了、磁盘还在」的幽灵。
        fs.rmSync(this.batchPath(target), { force: true });
      } catch (err) {
        throw new BatchError(`批次文件删除失败：${(err as Error).message}`, 500);
      }
      this.batches.delete(target);
      const key = normNo(batch.batch_no);
      // 只摘掉指向本批次的号码索引；批次号重复时旧批次仍占着索引，不能连带删掉。
      if (key && this.byNo.get(key) === target) this.byNo.delete(key);
      return { id: target };
    });
  }

  // ---------------------------------------------------------------- #6 列表

  async listFor(user: AuthedUser, allUsers = false, owner = ''): Promise<BatchView[]> {
    const filterOwner = allUsers ? String(owner || '') : '';
    return this.mutex.run(() => {
      const rows: BatchView[] = [];
      for (const batch of this.batches.values()) {
        if (!allUsers && batch.owner !== user?.username) continue;
        if (filterOwner && batch.owner !== filterOwner) continue;
        this.reconcile(batch);
        rows.push(this.publicView(batch));
      }
      rows.sort(
        (a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0),
      );
      return rows;
    });
  }

  // ---------------------------------------------------------------- #7 详情

  async detail(
    batchId: string,
    user: AuthedUser,
    itemState = '',
  ): Promise<{ batch: BatchView; items: BatchItemView[] }> {
    if (itemState && !(ITEM_STATES as readonly string[]).includes(itemState)) {
      throw new BatchError('条目状态只能是 pending/producing/done/failed。');
    }
    await this.ensureCatalog();
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      this.reconcile(batch);
      const data = this.publicView(batch);
      // 详情页要能直接显示「再生产这批大概多少钱」，不必先拉候选。
      data.cost_estimate = this.moneyView(user, this.estimateForPending(batch));
      return { batch: data, items: this.publicItems(batch, itemState) };
    });
  }

  // ---------------------------------------------------------------- #10 整批打包

  /**
   * 按时间顺序取这个批次名下的全部 job：runs 为主，items / batch_ref 兜底。
   *
   * 一个批次会分多轮生产（produce / retry_failed 各建一个 job），items 里只留
   * 最后一轮的 job_id；只扫 items 会漏掉前面几轮已经出片的视频。
   * 不再按 lane 过滤：批次零头/重试只有 1–3 条时会被判成 lane=small，
   * 但它们同样是这个批次的产物（见 historyLane）。
   */
  private batchJobs(batch: Batch): Job[] {
    const ordered: string[] = [];
    for (const run of batch.runs ?? []) {
      const jobId = String(run.job_id || '');
      if (jobId && !ordered.includes(jobId)) ordered.push(jobId);
    }
    for (const item of batch.items ?? []) {
      const jobId = String(item.job_id || '');
      if (jobId && !ordered.includes(jobId)) ordered.push(jobId);
    }
    if (!ordered.length) {
      // 老批次没有 runs：按 batch_ref 兜底，按创建时间排，保证后面覆盖前面。
      const linked = this.allJobs().filter((job) => String(job.batch_ref || '') === String(batch.id));
      linked.sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0));
      return linked;
    }
    const out: Job[] = [];
    for (const jobId of ordered) {
      const job = this.jobs.get(jobId);
      if (job) out.push(job);
    }
    return out;
  }

  /**
   * 按批次收集全部已出片视频：`{id, batch_no, files: [[zip 名, 本地路径]]}`。
   *
   * 资格：批次存在且属于本人（管理员不限）。小批量试跑不建批次，永远没有 batch_ref；
   * 批次自己的零头/重试即使被判成 lane=small，也照常收进整批包。
   * 口径：遍历每一轮 run 的成功记录，按场景 id 去重、保留最新一次成功的视频。
   */
  async batchVideos(batchId: string, user: AuthedUser): Promise<BatchVideoInfo> {
    return this.mutex.run(() => {
      const batch = this.authorized(batchId, user);
      this.reconcile(batch);
      const found = new Map<string, string>();
      for (const job of this.batchJobs(batch)) {
        const jobId = String(job.id || '');
        let out = this.jobs.outputPath(jobId);
        if (!out && job.out_dir) out = relocate(job.out_dir, this.config.dataDir);
        if (!out) continue;
        for (const rec of job.records ?? []) {
          if (String(rec.status || '') !== 'success') continue;
          const tid = String(rec.id || '');
          // 场景 id 要进 zip 条目名，先挡掉路径穿越与异常字符。
          if (!tid || !/^[A-Za-z0-9_.-]{1,120}$/.test(tid)) continue;
          const video = path.join(out, `${tid}.mp4`);
          if (isFile(video)) found.set(tid, video);
        }
      }
      const files: Array<[string, string]> = [...found.entries()]
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([tid, file]) => [`${tid}.mp4`, file]);
      return {
        id: String(batch.id || ''),
        batch_no: String(batch.batch_no || ''),
        files,
      };
    });
  }

  async zipPath(batchId: string, user: AuthedUser, files?: Array<[string, string]>): Promise<string> {
    const list = files ?? (await this.batchVideos(batchId, user)).files;
    if (!list.length) throw new BatchError('暂时没有可下载的视频。', 404);
    // 每次打包一个唯一文件名：固定名会让同一批次的两个并发下载互相 truncate，
    // 客户端拿到半截 zip（调用方负责发完就删）。
    const zipPath = path.join(
      this.config.dataDir,
      'tmp',
      `batch-${crypto.randomBytes(5).toString('hex')}.zip`,
    );
    fs.mkdirSync(path.dirname(zipPath), { recursive: true });
    try {
      fs.writeFileSync(zipPath, zipFiles(list.map(([name, file]) => ({ name, file }))));
    } catch (err) {
      try {
        fs.rmSync(zipPath, { force: true });
      } catch {
        /* 尽力而为 */
      }
      throw new BatchError(`打包失败：${(err as Error).message}`, 500);
    }
    return zipPath;
  }

  // ---------------------------------------------------------------- #11 历史分组

  /**
   * 按来源聚合历史任务：批次、账户、日期三种视角。
   *
   * groupBy="batch"（默认）：按 batch_ref 聚合 job；分不到现存批次的（试跑 + 老任务
   * + 批次已删除）进「未分组」。归属只认批次文件还在不在：job.batch_ref 不回写，
   * 批次一旦被删除，它名下的 job 会自动落回未分组。
   * groupBy="owner"（仅管理员看全部用户时生效）：一个账户的全部任务收进
   * `__owner__:<owner>` 一张卡。
   * groupBy="day"：忽略批次，全部按提交日期拆成 `__day__:<YYYY-MM-DD>`。
   * lane="small" / "big"：只统计该来源的 job（批次零头/重试算 big）。
   * 虚拟 key 只存在于接口层，job / batch 的落盘结构一律不动。
   */
  async historyGroups(
    user: AuthedUser,
    allUsers = false,
    owner = '',
    groupBy = 'batch',
    lane = '',
  ): Promise<HistoryGroupRow[]> {
    const filterOwner = allUsers ? String(owner || '') : '';
    const splitOwner = Boolean(allUsers && groupBy === 'owner');
    const byDay = groupBy === 'day';
    let laneKey = String(lane || '')
      .trim()
      .toLowerCase();
    if (laneKey !== 'small' && laneKey !== 'big') laneKey = '';
    return this.mutex.run(() => {
      const groups = new Map<string, InternalGroup>();
      for (const job of this.visibleJobs(user, allUsers, filterOwner)) {
        if (laneKey && historyLane(job) !== laneKey) continue;
        const ref = String(job.batch_ref || '');
        let key: string;
        if (byDay) key = `${DAY_KEY}:${dayKey(job.created_at)}`;
        else if (splitOwner) key = `${OWNER_KEY}:${String(job.owner || 'unknown')}`;
        else if (ref && this.batches.has(ref)) key = ref;
        else key = UNGROUPED_KEY;
        let slot = groups.get(key);
        if (!slot) {
          slot = {
            key,
            job_count: 0,
            total: 0,
            success: 0,
            failed: 0,
            unfinished: 0,
            first_at: null,
            last_at: null,
            owners: new Set(),
            sig: [],
            small_jobs: 0,
            big_jobs: 0,
          };
          groups.set(key, slot);
        }
        slot.job_count += 1;
        if (historyLane(job) === 'small') slot.small_jobs += 1;
        else slot.big_jobs += 1;
        slot.owners.add(String(job.owner || ''));
        // 明细状态指纹：前端用它判断已展开的分组要不要重载。
        // 只看成功/失败计数会漏掉「A 条 running→success、B 条 success→running」这种互相抵消的变化。
        slot.sig.push(
          `${job.id}:${job.status}:` +
            (job.records ?? [])
              .map((rec) => `${String(rec.id)}=${String(rec.status || '')}`)
              .join(','),
        );
        const created = Number(job.created_at || 0);
        const stamp = Number(job.finished_at || created);
        slot.first_at = slot.first_at === null ? created : Math.min(slot.first_at, created);
        slot.last_at = slot.last_at === null ? stamp : Math.max(slot.last_at, stamp);
        for (const rec of job.records ?? []) {
          const status = String(rec.status || '');
          slot.total += 1;
          // 与 jobs.work_for / 账户用量完全同口径：非成功终态全算失败侧，
          // 其余（running/queued/pending/空）算进行中，保证 total 三者之和。
          if (status === 'success') slot.success += 1;
          else if (FAILED_RECORD_STATES.has(status)) slot.failed += 1;
          else slot.unfinished += 1;
        }
      }
      const rows: HistoryGroupRow[] = [];
      for (const [key, slot] of groups) {
        const isDayGroup = key.startsWith(`${DAY_KEY}:`);
        const isOwnerGroup = key.startsWith(`${OWNER_KEY}:`);
        const isUngrouped = key === UNGROUPED_KEY || isOwnerGroup || isDayGroup;
        const batch = isUngrouped ? undefined : this.batches.get(key);
        // 管理员看全部时，分组卡片要能看出这批是谁跑的；多人混在一起就标「多人」。
        const owners = [...slot.owners].filter(Boolean);
        const ownerLabel = owners.length === 1 ? owners[0] : owners.length ? '多人' : '';
        rows.push({
          key,
          day: isDayGroup ? key.slice(DAY_KEY.length + 1) : '',
          // 按日期的组没有批次号；按账户的组混着批次任务，批次号逐行解析。
          batch_no: isDayGroup || isOwnerGroup ? '' : isUngrouped ? '未分组' : String(batch?.batch_no || ''),
          group_kind: isDayGroup ? 'day' : isOwnerGroup ? 'owner' : 'batch',
          is_ungrouped: isUngrouped,
          small_jobs: slot.small_jobs,
          big_jobs: slot.big_jobs,
          owner: ownerLabel,
          job_count: slot.job_count,
          total: slot.total,
          success: slot.success,
          failed: slot.failed,
          unfinished: slot.unfinished,
          state_sig: crypto
            .createHash('md5')
            .update([...slot.sig].sort().join('|'), 'utf8')
            .digest('hex')
            .slice(0, 12),
          first_at: slot.first_at,
          last_at: slot.last_at || 0,
        });
      }
      // 统一按最近更新倒序：以前把「未分组」钉在最后，最新的试跑反而被压在一堆老批次下面。
      rows.sort((a, b) => (b.last_at || 0) - (a.last_at || 0));
      return rows;
    });
  }

  /**
   * 空批次（还没有任何生产记录）不生成一个点不开的分组，只给个计数。
   * 只按「有没有 job 的 batch_ref 指向它」算，与分组方式 / lane 无关。
   */
  async emptyBatchCount(user: AuthedUser, allUsers = false, owner = ''): Promise<number> {
    return this.mutex.run(() => {
      const used = new Set(this.allJobs().map((job) => String(job.batch_ref || '')));
      let count = 0;
      for (const [key, batch] of this.batches) {
        if (!allUsers) {
          if (batch.owner !== user?.username) continue;
        } else if (owner && batch.owner !== owner) {
          continue;
        }
        if (!used.has(key)) count += 1;
      }
      return count;
    });
  }

  /** 该轮任务提交时快照下来的提示词；job 已被删就返回空（不猜、不拿批次当前值冒充）。 */
  private runPrompt(jobId: string): string {
    if (!jobId) return '';
    const job = this.jobs.get(String(jobId));
    return String(job?.prompt || '');
  }

  /**
   * 批次每一轮的生产视图：序号、条数、时间、这一轮实际用的提示词、是否改过。
   *
   * `runs` 里只有 job_id 与计数，没有提示词快照（老数据更没有）；提示词从该轮 job 的
   * `prompt` 还原 —— 那是提交那一刻的快照，锁定后改提示词只影响之后的轮次。
   */
  runViews(batch: Batch): BatchRunView[] {
    const views: BatchRunView[] = [];
    let previous: string | null = null;
    for (const run of batch.runs ?? []) {
      if (!isRecord(run)) continue;
      const jobId = String(run.job_id || '');
      const prompt = this.runPrompt(jobId);
      views.push({
        no: views.length + 1,
        job_id: jobId,
        count: asIntOr(run.count, 0) || 0,
        started_at: Number(run.started_at ?? 0) || 0,
        finished_at: Number(run.finished_at ?? 0) || 0,
        success: asIntOr(run.success, 0) || 0,
        failed: asIntOr(run.failed, 0) || 0,
        prompt,
        prompt_changed: Boolean(prompt) && previous !== null && prompt !== previous,
      });
      if (prompt) previous = prompt;
    }
    return views;
  }

  /**
   * 分组明细。allUsers 显式传 false 时（管理员「只看我的任务」）连明细也只列自己，
   * 否则卡片写 9 条、点开却是全账户 20 条，数字对不上。null = 沿用旧行为，兼容离线调用。
   * lane 与 historyGroups 同口径，避免同一个分组 key 在小批量栏混进大批量的记录。
   */
  async historyItems(
    key: string,
    user: AuthedUser,
    offset = 0,
    limit = 200,
    owner = '',
    allUsers: boolean | null = null,
    lane = '',
  ): Promise<HistoryItemsResult> {
    const safeOffset = Math.max(0, Math.trunc(offset || 0));
    const safeLimit = Math.max(1, Math.min(500, Math.trunc(limit || 200)));
    let filterOwner = user?.role === 'admin' ? String(owner || '') : '';
    if (allUsers === false) filterOwner = String(user?.username || '');
    let laneKey = String(lane || '')
      .trim()
      .toLowerCase();
    if (laneKey !== 'small' && laneKey !== 'big') laneKey = '';
    const groupKey = String(key ?? '');

    return this.mutex.run(() => {
      let batch: Batch | undefined;
      let batchNo = '';
      let jobs: Job[];
      if (groupKey.startsWith(`${DAY_KEY}:`)) {
        // 按日期分组：忽略批次归属，取这一天的全部可见 job。
        const targetDay = groupKey.slice(DAY_KEY.length + 1);
        batchNo = '';
        jobs = this.allJobs().filter((job) => dayKey(job.created_at) === targetDay);
      } else if (groupKey.startsWith(`${OWNER_KEY}:`)) {
        // 管理员按账户分出来的组：该账户的全部任务（批次任务 + 试跑）。
        if (user?.role !== 'admin') throw new BatchError('无权查看该分组。', 403);
        const targetOwner = groupKey.slice(OWNER_KEY.length + 1);
        batchNo = '';
        // 筛选与虚拟组冲突时返回空，不能放宽到别的账户
        jobs =
          filterOwner && filterOwner !== targetOwner
            ? []
            : this.allJobs().filter((job) => String(job.owner || '') === targetOwner);
      } else if (groupKey.startsWith(`${UNGROUPED_KEY}:`)) {
        // 旧版按账户拆「未分组」的 key（页面未刷新时还会请求一次）：
        // 只取该账户、且不属于任何现存批次的任务。新分组不再产出这个 key。
        if (user?.role !== 'admin') throw new BatchError('无权查看该分组。', 403);
        const targetOwner = groupKey.slice(UNGROUPED_KEY.length + 1);
        batchNo = '未分组';
        jobs =
          filterOwner && filterOwner !== targetOwner
            ? []
            : this.allJobs().filter(
                (job) =>
                  (!job.batch_ref || !this.batches.has(String(job.batch_ref))) &&
                  String(job.owner || '') === targetOwner,
              );
      } else if (groupKey !== UNGROUPED_KEY) {
        batch = this.batches.get(groupKey);
        if (!batch) throw new BatchError('找不到该批次。', 404);
        if (user?.role !== 'admin' && batch.owner !== user?.username) {
          throw new BatchError('无权查看该批次。', 403);
        }
        batchNo = String(batch.batch_no || '');
        jobs = this.allJobs().filter((job) => String(job.batch_ref || '') === groupKey);
      } else {
        batchNo = '未分组';
        // 与 historyGroups 同口径：没有 batch_ref，或 batch_ref 指向的批次已不存在。
        let mine = this.allJobs().filter(
          (job) => !job.batch_ref || !this.batches.has(String(job.batch_ref)),
        );
        if (user?.role !== 'admin') {
          mine = mine.filter((job) => job.owner === user?.username);
        }
        jobs = mine;
      }
      if (laneKey) jobs = jobs.filter((job) => historyLane(job) === laneKey);
      // 「第几批 20 条」：runs 里每轮一条，提示词从该轮 job 的 prompt 还原。
      // 重试/零头会新建一轮，同一条模板可能出现在多轮里，所以按模板收集全部轮次。
      const runs = batch ? this.runViews(batch) : [];
      const runNoByJob = new Map<string, number>();
      for (const view of runs) if (view.job_id) runNoByJob.set(String(view.job_id), view.no);
      const runChanged = new Map<number, boolean>();
      for (const view of runs) runChanged.set(view.no, Boolean(view.prompt_changed));
      const runNosByTemplate = new Map<string, number[]>();
      for (const view of runs) {
        const source = this.jobs.get(String(view.job_id || ''));
        let ids = (source?.templates ?? []).filter(isRecord).map((tpl) => String(tpl.id));
        if (!ids.length) ids = (source?.records ?? []).map((rec) => String(rec.id));
        for (const templateId of ids) {
          const list = runNosByTemplate.get(templateId) ?? [];
          list.push(view.no);
          runNosByTemplate.set(templateId, list);
        }
      }
      const rows: HistoryRow[] = [];
      const ordered = [...jobs].sort(
        (a, b) => Number(a.created_at || 0) - Number(b.created_at || 0),
      );
      for (const job of ordered) {
        const jobOwner = job.owner;
        if (user?.role !== 'admin' && jobOwner !== user?.username) continue;
        if (filterOwner && jobOwner !== filterOwner) continue;
        // 按账户的组里混着批次任务与试跑：批次号逐行解析，指向已删除批次的留空。
        let rowBatchNo = batchNo;
        if (groupKey.startsWith(`${OWNER_KEY}:`)) {
          const ref = String(job.batch_ref || '');
          const linked = ref ? this.batches.get(ref) : undefined;
          rowBatchNo = String(linked?.batch_no || '');
        }
        const names = new Map<string, string>();
        for (const tpl of job.templates ?? []) {
          if (isRecord(tpl)) names.set(String(tpl.id), String(tpl.name || ''));
        }
        // 这条记录对应的定妆图 = job 自己的素材图。只有落在 uploads/<xx>/<id>.<ext> 的
        // 才走带权限校验的 /api/uploads 接口，其它来源一律不给 URL。
        let imageUrl = '';
        const jobSlots = imageSlots(job, this.config.dataDir);
        let firstId = String(jobSlots[0]?.upload_id ?? '');
        if (!firstId) {
          const hit = UPLOAD_PATH_RE.exec(String(job.image_path || ''));
          firstId = hit ? String(hit[1]) : '';
        }
        if (firstId) imageUrl = `/api/uploads/${firstId}`;
        const jobRunNo = runNoByJob.get(String(job.id)) ?? 0;
        for (const rec of job.records ?? []) {
          const templateId = String(rec.id);
          const status = String(rec.status || '');
          const rawName = rec.template;
          // 老任务/模板下架后 job.templates 里没有名字，退回记录自带的 template 字段；
          // 它可能是完整云端 URL，只取文件名展示。
          const fallbackName =
            typeof rawName === 'string' ? (rawName.split('/').pop() ?? '') : '';
          const templateName = names.get(templateId) || fallbackName;
          const byTemplate = runNosByTemplate.get(templateId);
          const nosSource = byTemplate ?? (jobRunNo ? [jobRunNo] : []);
          rows.push({
            job_id: String(job.id),
            template_id: templateId,
            // 这一条属于第几批：run_no 是它最近一次生产所在轮次，
            // run_nos 列出它出现过的全部轮次（重试会让同一条落在多轮）。
            run_no: jobRunNo,
            run_nos: [...new Set(nosSource)].sort((a, b) => a - b),
            run_total: runs.length,
            run_prompt_changed: runChanged.get(jobRunNo) ?? false,
            image_url: imageUrl,
            template_name: templateName,
            // 管理员看全部用户时，明细行要能分辨是谁、什么时候、属于哪批。
            owner: String(jobOwner || ''),
            created_at: Number(job.created_at || 0),
            batch_no: rowBatchNo,
            job_status: String(job.status || ''),
            status,
            seconds: rec.seconds ?? null,
            download_url: status === 'success' ? `/api/jobs/${String(job.id)}/download/${templateId}` : '',
            archive_warning: rawField(rec, 'archive_error')
              ? '视频已生成，但 COS 归档失败，请联系管理员。'
              : '',
            // 与批次条目同口径：done 的质量提示，状态仍是 success。
            verify_warn: redact(
              isRecord(rec.verify) ? String(rec.verify.warn ?? '') : '',
            ).slice(0, 200),
          });
        }
      }
      const total = rows.length;
      return {
        key: groupKey,
        batch_no: batchNo,
        items: rows.slice(safeOffset, safeOffset + safeLimit),
        runs,
        total,
        offset: safeOffset,
        limit: safeLimit,
      };
    });
  }
}

// ---------------------------------------------------------------- 尾部工具

function isFile(file: string): boolean {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

/** 与 `TemplateCatalogService` 内部同一套编号归一：纯数字补到 3 位。 */
function normalizeId(value: unknown): string {
  const text = String(value ?? '').trim();
  return /^\d+$/.test(text) ? text.padStart(3, '0') : text;
}
