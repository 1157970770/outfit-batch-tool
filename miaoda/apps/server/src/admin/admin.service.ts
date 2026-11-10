/**
 * 管理端业务逻辑：1:1 移植 Python `app/web/server.py` 的 `/api/admin/*` 处理器。
 *
 * 口径对齐的 Python 函数（只读参考，不修改）：
 * - `_admin_status`（server.py 第 695 行起）
 * - `_admin_template_health`（第 719 行起）
 * - `_admin_usage`（第 1669 行）、`jobs.usage_for`（jobs.py 第 998 行）
 * - `_admin_archive_search`（第 1681 行）、`_admin_archive_sign`（第 1745 行）
 * - `_account_work` / `_send_account_csv`（第 1779 / 1806 行）、`jobs.work_for`（jobs.py 第 1078 行）
 * - `_admin_add_user` / `_admin_reset_user` / `_admin_set_quota` / `_admin_disable_user`
 *   / `_admin_enable_user` / `_admin_delete_user`（第 1842–1934 行）
 *
 * 两条与 Python 现状不同的显式约定（保持 TS 侧行为可预期，已在交接说明里列出）：
 * 1. COS 还没接入（没有 storage 适配器）。`archive-search` 先按本机任务账本（记录的
 *    cos_key）命中；账本为空时**不伪造云端结果**，直接返回 503，文案沿用 Python
 *    `_admin_archive_sign` 的「云端归档未启用，无法生成下载链接。」。
 * 2. `GET /api/admin/users` 在 Python 的 5 个字段（username/role/enabled/quota/used）
 *    之外多带一个 `expires`：需求明确要展示到期时间，Python 该接口没下发。
 */
import * as fs from 'node:fs';
import { HttpException, HttpStatus, Injectable, Logger } from '@nestjs/common';

import { WEB_VERSION } from '../api/health.controller';
import { makePassword } from '../auth/password';
// 注意：这些必须是「值导入」而不是 `import type`——Nest 靠 emitDecoratorMetadata 的
// 运行时构造函数元数据做依赖注入，写成 type-only 会让元数据退化成 Function，启动即报
// 「Nest can't resolve dependencies of the AdminService (?, Function, Function, Function)」。
import { BatchManagerService } from '../batches/batch-manager.service';
import { writeJsonAtomic } from '../common/fs-json';
import { AppConfigService, type UserRecord } from '../config/app-config.service';
import type { Job } from '../jobs/job-manager.service';
import { JobManagerService } from '../jobs/job-manager.service';
import { TemplateCatalogService } from '../templates/template-catalog.service';

/** 管理端接口的查询串：Nest 给的是 `string | string[] | undefined`，统一按首值取。 */
export type AdminQuery = Record<string, unknown>;

/** 统计口径里的「失败侧」（jobs.py:165 `_FAILED_RECORD_STATES`）。 */
const FAILED_RECORD_STATES = new Set(['failed', 'invalid', 'expired', 'canceled', 'unknown', 'timeout', 'skipped']);

/** `_account_work` 允许的状态筛选：其余一律按「不筛选」处理。 */
const WORK_STATUSES = new Set(['', 'success', 'failed', 'invalid', 'expired', 'canceled', 'running']);

/** `/api/admin/users` 可接受的 username：对应 Python `^[\w.-]{2,32}$`（unicode）。 */
const USERNAME_RE = /^[\p{L}\p{N}_.-]{2,32}$/u;

/** COS 未接入时的统一文案（Python `_admin_archive_sign` 原文）。 */
export const ARCHIVE_DISABLED_TEXT = '云端归档未启用，无法生成下载链接。';

export function adminError(message: string, status: number = HttpStatus.BAD_REQUEST): HttpException {
  return new HttpException({ ok: false, error: message }, status);
}

/** 取查询串首值（等价 Python `parse_qs(...).get(key, [""])[0]`）。 */
function firstParam(value: unknown): string {
  if (Array.isArray(value)) return String(value[0] ?? '');
  if (value === undefined || value === null) return '';
  return String(value);
}

/** 严格整数：Python `int()` 的近似（允许首尾空白与正负号，拒绝小数/空串）。 */
function toIntStrict(raw: unknown): number | null {
  if (typeof raw === 'boolean') return raw ? 1 : 0;
  if (typeof raw === 'number') return Number.isFinite(raw) ? Math.trunc(raw) : null;
  const text = String(raw ?? '').trim();
  if (!/^[+-]?\d+$/.test(text)) return null;
  return Number(text);
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

/** 时间戳（秒）→ 本地 `YYYY-MM-DD HH:mm:ss`，与 Python `time.strftime` 同格式。 */
function localStamp(seconds: number): string {
  const date = new Date(seconds * 1000);
  if (!Number.isFinite(date.getTime())) return '';
  const pad = (value: number): string => String(value).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** `YYYY-MM-DD` → 当天 0 点（本地时区，秒）；非法输入按「不限」返回 null。 */
function dayStart(raw: unknown): number | null {
  const text = String(raw ?? '').trim();
  if (!text) return null;
  const match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(text);
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const time = date.getTime();
  return Number.isFinite(time) ? time / 1000 : null;
}

/** 与 Python `csv.writer` 默认的最小引号策略一致。 */
function csvCell(text: string): string {
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** 防 Excel 公式注入：`= + - @ \t \r` 开头的单元格前置单引号。 */
function csvSafe(value: unknown): string {
  const text = String(value ?? '');
  return /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
}

function asText(value: unknown): string {
  return value === undefined || value === null ? '' : String(value);
}

function asCount(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

/** 记录级字段访问：JobRecord 的类型里没有归档/时间字段，这里按原始字典读。 */
function recordOf(record: unknown): Record<string, unknown> {
  return (record ?? {}) as Record<string, unknown>;
}

export interface UsageRow {
  owner: string;
  role: string;
  enabled: boolean;
  jobs: number;
  items: number;
  success: number;
  failed: number;
  unfinished: number;
  timeout: number;
  billable_seconds: number;
  estimated_cost: number;
  last_at: number;
  success_rate: number;
}

export interface WorkRow {
  created_at: number;
  finished_at: number | null;
  job_id: string;
  batch_id: string;
  batch_ref: string;
  batch_no: string;
  template_id: string;
  template_name: string;
  status: string;
  seconds: number | null;
  billable_seconds: number | null;
  estimated_cost: number | null;
  cost_source: 'record' | 'derived' | 'missing';
  resolution: string;
  duration: number | null;
}

interface WorkResult {
  owner: string;
  total: number;
  success: number;
  failed: number;
  unfinished: number;
  offset: number;
  limit: number;
  items: WorkRow[];
  total_billable_seconds: number;
  total_cost: number;
}

export interface CsvPayload {
  filename: string;
  body: string;
}

@Injectable()
export class AdminService {
  private readonly logger = new Logger('Admin');

  constructor(
    private readonly config: AppConfigService,
    private readonly jobs: JobManagerService,
    private readonly batches: BatchManagerService,
    private readonly catalog: TemplateCatalogService,
  ) {}

  // ---------------------------------------------------------------- 状态 / 模板健康

  /** `GET /api/admin/status`：版本、数据目录余量、任务计数、上限、账户一览。 */
  status(): Record<string, unknown> {
    const all = this.jobs.list('', true, '');
    const queued = all.filter((job) => job.status === 'queued').length;
    const running = all.filter((job) => job.status === 'running');
    const smallActive = running.filter((job) => job.lane === 'small').length;
    const bigActive = running.length - smallActive;
    const web = this.config.web as { users?: Record<string, UserRecord> };
    const users = Object.entries(web.users ?? {}).map(([username, record]) => ({
      username,
      role: asText(record?.role) || 'user',
      // Python `u.get("enabled", True)`：只有显式 false 才算停用
      enabled: record?.enabled === undefined ? true : Boolean(record.enabled),
    }));
    return {
      ok: true,
      version: WEB_VERSION,
      templates: this.catalog.items.length,
      queued,
      active: running.length,
      big_active: bigActive,
      small_active: smallActive,
      free_gb: round(this.freeBytes() / 1024 ** 3, 1),
      max_concurrency: this.config.maxConcurrency,
      max_active_per_user: this.config.maxActivePerUser,
      small_lane: {
        items: this.config.smallLaneItems,
        max_jobs: this.config.maxActiveSmallJobs,
        inflight: this.config.smallLaneInflight,
      },
      limits: {
        query_rpm: this.config.submitRpm,
        query_rps: this.config.queryQps,
        max_inflight: this.config.maxInflight,
        small_max_inflight: this.config.smallLaneInflight,
      },
      users,
    };
  }

  /** `GET /api/admin/template-health`：候选集 / 未打标 / 弃用 / 缺文件等体检数字。 */
  templateHealth(): Record<string, unknown> {
    const info = this.catalog.info as Record<string, unknown>;
    const workbookRows = asCount(info.workbook_rows);
    const sourceReady = asCount(info.source_ready_templates);
    return {
      ok: true,
      workbook_rows: workbookRows,
      tagged_rows: asCount(info.matched_templates),
      source_ready: sourceReady,
      unmatched: asCount(info.manifest_unmatched),
      untagged: asCount(info.untagged_skipped),
      ready_ratio: workbookRows ? round(sourceReady / workbookRows, 4) : 0,
      manifest_warning: asText(info.manifest_warning),
    };
  }

  /** `POST /api/admin/reload-templates`：强制重建候选集（含重新拉 manifest）。 */
  async reloadTemplates(): Promise<Record<string, unknown>> {
    try {
      await this.catalog.build(true);
    } catch (err) {
      throw adminError(`重载失败：${(err as Error).message}`, HttpStatus.SERVICE_UNAVAILABLE);
    }
    const info = this.catalog.info as Record<string, unknown>;
    return {
      ok: true,
      count: this.catalog.items.length,
      source_ready_templates: asCount(info.source_ready_templates),
      manifest_unmatched: asCount(info.manifest_unmatched),
      untagged_skipped: asCount(info.untagged_skipped),
      manifest_warning: asText(info.manifest_warning),
    };
  }

  // ---------------------------------------------------------------- 用量 / 明细

  /**
   * `GET /api/admin/usage`：按账户汇总。
   * 金额只允许出现在这里与账户明细，别往任务接口里加。
   */
  usage(query: AdminQuery = {}): Record<string, unknown> {
    const owner = firstParam(query.owner).trim();
    const start = dayStart(firstParam(query.from));
    const endDay = dayStart(firstParam(query.to));
    const end = endDay === null ? null : endDay + 86400; // 含结束当天
    const data = this.usageFor(start, end, owner);
    return {
      ok: true,
      ...data,
      owner,
      from: firstParam(query.from),
      to: firstParam(query.to),
    };
  }

  private usageFor(
    start: number | null,
    end: number | null,
    owner: string,
  ): { accounts: UsageRow[]; totals: Record<string, number> } {
    const blank = (name: string, role = 'user', enabled = true): UsageRow => ({
      owner: name,
      role,
      enabled,
      jobs: 0,
      items: 0,
      success: 0,
      failed: 0,
      unfinished: 0,
      timeout: 0,
      billable_seconds: 0,
      estimated_cost: 0,
      last_at: 0,
      success_rate: 0,
    });

    const rows = new Map<string, UsageRow>();
    const web = this.config.web as { users?: Record<string, UserRecord> };
    for (const [name, record] of Object.entries(web.users ?? {})) {
      rows.set(name, blank(name, asText(record?.role) || 'user', record?.enabled === undefined ? true : Boolean(record.enabled)));
    }

    for (const job of this.jobs.list('', true, '')) {
      const name = asText(job.owner) || '未知';
      if (owner && name !== owner) continue;
      const created = Number(job.created_at) || 0;
      if (start && created < start) continue;
      if (end && created >= end) continue;
      const row = rows.get(name) ?? blank(name);
      rows.set(name, row);
      row.jobs += 1;
      if (job.status === 'queued' || job.status === 'running') row.unfinished += 1;
      for (const raw of job.records ?? []) {
        const rec = recordOf(raw);
        const status = asText(rec.status);
        row.items += 1;
        if (status === 'success') {
          row.success += 1;
        } else if (status === 'timeout') {
          // 超时单列：业务上没拿到片子算失败，但云端可能已计费，管理员要能单独看
          row.failed += 1;
          row.timeout += 1;
        } else if (FAILED_RECORD_STATES.has(status)) {
          row.failed += 1;
        } else if (status === 'running') {
          row.unfinished += 1;
        }
        const billable = Number(rec.billable_seconds ?? 0);
        if (Number.isFinite(billable)) row.billable_seconds += billable;
        row.estimated_cost += this.recordCost(rec, job).value;
      }
      row.last_at = Math.max(row.last_at, created, Number(job.finished_at) || 0);
    }

    const accounts = [...rows.values()].map((row) => ({
      ...row,
      success_rate: row.items ? round(row.success / row.items, 4) : 0,
      billable_seconds: round(row.billable_seconds, 2),
      estimated_cost: round(row.estimated_cost, 2),
    }));
    accounts.sort((a, b) => {
      if (b.estimated_cost !== a.estimated_cost) return b.estimated_cost - a.estimated_cost;
      if (b.items !== a.items) return b.items - a.items;
      return a.owner < b.owner ? -1 : a.owner > b.owner ? 1 : 0;
    });

    const totals: Record<string, number> = {
      jobs: 0,
      items: 0,
      success: 0,
      failed: 0,
      unfinished: 0,
      timeout: 0,
    };
    for (const row of accounts) {
      for (const key of Object.keys(totals)) {
        totals[key] += Math.trunc(row[key as keyof UsageRow] as number);
      }
    }
    totals.billable_seconds = round(
      accounts.reduce((sum, row) => sum + row.billable_seconds, 0),
      2,
    );
    totals.estimated_cost = round(
      accounts.reduce((sum, row) => sum + row.estimated_cost, 0),
      2,
    );
    totals.accounts = accounts.length;
    return { accounts, totals };
  }

  /**
   * 单条记录的费用：优先用生成时落下的 `estimated_cost`；
   * 老数据（单价还是 null 时生成的）按当前单价回算，并标注 `derived`。
   */
  private recordCost(rec: Record<string, unknown>, job: Job): { value: number; source: WorkRow['cost_source'] } {
    const raw = rec.estimated_cost;
    if (raw !== null && raw !== undefined && raw !== '') {
      const value = Number(raw);
      if (Number.isFinite(value)) return { value, source: 'record' };
    }
    const seconds = Number(rec.billable_seconds ?? 0);
    const billable = Number.isFinite(seconds) ? seconds : 0;
    const { price } = this.config.unitPrice(asText(job.resolution));
    if (billable > 0 && price) return { value: billable * Number(price), source: 'derived' };
    return { value: 0, source: 'missing' };
  }

  /** 账户已占用的生成额度（条）：成功 + 进行中 + 排队任务里还没跑到的条目。 */
  videosUsed(owner: string): number {
    let used = 0;
    for (const job of this.jobs.list('', true, owner)) {
      if (asText(job.owner) !== owner) continue;
      let counted = 0;
      for (const raw of job.records ?? []) {
        const status = asText(recordOf(raw).status);
        if (status === 'success' || status === 'running') counted += 1;
      }
      if (job.status === 'queued' || job.status === 'running') {
        counted += Math.max(0, asCount(job.total) - counted);
      }
      used += counted;
    }
    return used;
  }

  // ---------------------------------------------------------------- 账户明细

  /** `GET /api/admin/accounts/<owner>/work`：逐条工作明细（分页）。 */
  async accountWork(owner: string, query: AdminQuery = {}): Promise<Record<string, unknown>> {
    const result = await this.workRows(owner, query);
    const offset = result.offset;
    const limit = result.limit;
    return {
      ok: true,
      owner: result.owner,
      total: result.total,
      success: result.success,
      failed: result.failed,
      unfinished: result.unfinished,
      offset,
      limit,
      items: result.items.slice(offset, offset + limit),
      total_billable_seconds: result.total_billable_seconds,
      total_cost: result.total_cost,
      status: this.workStatus(query),
      from: firstParam(query.from),
      to: firstParam(query.to),
    };
  }

  /** `GET .../work.csv` 或 `?format=csv`：全量明细导出（不受分页上限影响）。 */
  async accountCsv(owner: string, query: AdminQuery = {}): Promise<CsvPayload> {
    const result = await this.workRows(owner, query);
    const statusLabels: Record<string, string> = {
      success: '成功',
      failed: '失败',
      invalid: '无效',
      expired: '过期',
      canceled: '已取消',
      running: '进行中',
    };
    const costLabels: Record<string, string> = { record: '实测', derived: '按当前单价回算', missing: '' };
    const lines: string[] = [];
    const push = (cells: string[]): void => {
      lines.push(cells.map(csvCell).join(','));
    };
    push(['时间', '批次号', '模板ID', '模板名称', '状态', '计费秒', '预估金额(元)', '金额来源', '分辨率', '输出时长(秒)', '任务ID']);
    for (const row of result.items) {
      push([
        row.created_at ? localStamp(row.created_at) : '',
        csvSafe(row.batch_no),
        csvSafe(row.template_id),
        csvSafe(row.template_name),
        csvSafe(statusLabels[row.status] ?? row.status),
        row.billable_seconds === null || row.billable_seconds === undefined ? '' : String(row.billable_seconds),
        row.estimated_cost === null || row.estimated_cost === undefined ? '' : String(row.estimated_cost),
        csvSafe(costLabels[row.cost_source] ?? ''),
        csvSafe(row.resolution),
        row.duration === null || row.duration === undefined ? '' : String(row.duration),
        csvSafe(row.job_id),
      ]);
    }
    // 行尾与 Python csv.writer 一致用 CRLF；开头 BOM 让 Excel 认出 UTF-8
    const body = '\uFEFF' + lines.join('\r\n') + (lines.length ? '\r\n' : '');
    return { filename: `account-${owner}-work.csv`, body };
  }

  private workStatus(query: AdminQuery): string {
    const status = firstParam(query.status).trim();
    return WORK_STATUSES.has(status) ? status : '';
  }

  private async workRows(owner: string, query: AdminQuery): Promise<WorkResult> {
    const start = dayStart(firstParam(query.from));
    const endDay = dayStart(firstParam(query.to));
    const end = endDay === null ? null : endDay + 86400;
    const status = this.workStatus(query);
    const offsetRaw = toIntStrict(firstParam(query.offset));
    const limitRaw = toIntStrict(firstParam(query.limit));
    // Python 的默认值：offset 缺省 0、limit 缺省 100；非整数直接 400
    if ((firstParam(query.offset) !== '' && offsetRaw === null) || (firstParam(query.limit) !== '' && limitRaw === null)) {
      throw adminError('分页参数无效。');
    }
    const offset = Math.max(0, offsetRaw ?? 0);
    const limit = Math.max(1, Math.min(500, limitRaw ?? 100));

    const target = asText(owner);
    const rows: WorkRow[] = [];
    for (const job of this.jobs.list('', true, target)) {
      if (asText(job.owner) !== target) continue;
      const created = Number(job.created_at) || 0;
      if (start && created < start) continue;
      if (end && created >= end) continue;
      const names = new Map<string, string>();
      for (const tpl of job.templates ?? []) {
        if (!tpl || typeof tpl !== 'object') continue;
        names.set(String(tpl.id), asText(tpl.name));
      }
      const finishedAt = job.finished_at === null || job.finished_at === undefined ? null : Number(job.finished_at);
      for (const raw of job.records ?? []) {
        const rec = recordOf(raw);
        const recStatus = asText(rec.status);
        if (status && recStatus !== status) continue;
        const templateId = asText(rec.id);
        const cost = this.recordCost(rec, job);
        rows.push({
          created_at: created,
          finished_at: finishedAt,
          job_id: asText(job.id),
          batch_id: asText(job.batch_id),
          batch_ref: asText(job.batch_ref),
          batch_no: '',
          template_id: templateId,
          template_name: names.get(templateId) ?? '',
          status: recStatus,
          seconds: rec.seconds === undefined || rec.seconds === null ? null : Number(rec.seconds),
          billable_seconds:
            rec.billable_seconds === undefined || rec.billable_seconds === null ? null : Number(rec.billable_seconds),
          estimated_cost: cost.source === 'missing' ? null : round(cost.value, 4),
          cost_source: cost.source,
          resolution: asText(job.resolution),
          duration: job.duration === undefined || job.duration === null ? null : Number(job.duration),
        });
      }
    }

    // 排序键 (created_at, template_id) 倒序 —— 与 Python 的 sort(reverse=True) 等价
    rows.sort((a, b) => {
      if (a.created_at !== b.created_at) return a.created_at - b.created_at;
      return a.template_id < b.template_id ? -1 : a.template_id > b.template_id ? 1 : 0;
    });
    rows.reverse();

    // 批次号：明细行只存 batch_ref，编号得从批次表补
    const batchNos = await this.batchNos(rows.map((row) => row.batch_ref));
    for (const row of rows) row.batch_no = batchNos.get(row.batch_ref) ?? '';

    let success = 0;
    let failed = 0;
    let unfinished = 0;
    let billable = 0;
    let cost = 0;
    for (const row of rows) {
      if (row.status === 'success') success += 1;
      else if (FAILED_RECORD_STATES.has(row.status)) failed += 1;
      // 剩下的只有 running：用 else 兜底，保证 total = success + failed + unfinished
      else unfinished += 1;
      const secs = Number(row.billable_seconds ?? 0);
      if (Number.isFinite(secs)) billable += secs;
      const money = Number(row.estimated_cost ?? 0);
      if (Number.isFinite(money)) cost += money;
    }

    return {
      owner: target,
      total: rows.length,
      success,
      failed,
      unfinished,
      offset,
      limit,
      items: rows,
      total_billable_seconds: round(billable, 2),
      total_cost: round(cost, 2),
    };
  }

  private async batchNos(refs: string[]): Promise<Map<string, string>> {
    const map = new Map<string, string>();
    for (const ref of new Set(refs.filter(Boolean))) {
      try {
        const batch = await this.batches.get(ref);
        map.set(ref, asText(batch?.batch_no));
      } catch (err) {
        this.logger.warn(`查批次号失败（${ref}）：${(err as Error).message}`);
        map.set(ref, '');
      }
    }
    return map;
  }

  // ---------------------------------------------------------------- 归档（COS 未接入）

  /**
   * `GET /api/admin/archive-search?style=`（兼容 `?q=`）：先查本机任务账本里的 cos_key。
   * 账本为空时按需求返回 503「COS 未配置」同文案，不伪造云端列举结果。
   */
  async archiveSearch(query: AdminQuery = {}): Promise<Record<string, unknown>> {
    const style = (firstParam(query.style) || firstParam(query.q)).trim();
    if (!style) throw adminError('请输入要搜索的款号。');
    if (style.length > 60) throw adminError('款号过长。');
    const needle = style.toLowerCase();

    const entries: Array<Record<string, unknown>> = [];
    const seen = new Set<string>();
    for (const job of this.jobs.list('', true, '')) {
      const tags = (job.tags ?? {}) as Record<string, unknown>;
      const values = new Set<string>();
      for (const value of Object.values(tags)) {
        const text = asText(value).trim().toLowerCase();
        if (text) values.add(text);
      }
      const legacy = asText(job.style_no).trim().toLowerCase();
      if (legacy) values.add(legacy);
      if (!values.has(needle)) continue;
      for (const raw of job.records ?? []) {
        const rec = recordOf(raw);
        const key = asText(rec.cos_key).trim();
        if (!key || seen.has(key) || asText(rec.status) !== 'success') continue;
        seen.add(key);
        entries.push({
          key,
          job_id: job.id,
          record_id: rec.id,
          template_id: rec.id,
          owner: job.owner,
          batch_ref: job.batch_ref,
          created_at: job.finished_at ?? job.created_at,
          batch_no: '',
        });
      }
    }

    if (!entries.length) {
      // 账本没记上就是「没有归档记录」：Python 在这里去列 COS 前缀兜底，
      // 未接入 COS 时同样只会得到空列表 → 返回 200 + 空 entries，不伪造云端结果。
      return { ok: true, style, source: 'ledger', entries: [], total: 0 };
    }

    const batchNos = await this.batchNos(entries.map((entry) => asText(entry.batch_ref)));
    for (const entry of entries) entry.batch_no = batchNos.get(asText(entry.batch_ref)) ?? '';
    entries.sort((a, b) => (Number(b.created_at) || 0) - (Number(a.created_at) || 0));
    return { ok: true, style, source: 'ledger', entries, total: entries.length };
  }

  /** `POST /api/admin/archive-sign`：COS 未接入，参数校验通过后统一 503。 */
  archiveSign(body: Record<string, unknown> = {}): Record<string, unknown> {
    const rawKeys = body.keys;
    if (!Array.isArray(rawKeys) || !rawKeys.length) {
      throw adminError('请先勾选要生成链接的视频。');
    }
    const keys: string[] = [];
    for (const item of rawKeys.slice(0, 500)) {
      const key = asText(item).trim().replace(/^\/+/, '');
      if (key && !key.split('/').includes('..') && !keys.includes(key)) keys.push(key);
    }
    if (!keys.length) throw adminError('没有可用的对象路径。');
    // 没有 storage 适配器：不生成假链接
    throw adminError(ARCHIVE_DISABLED_TEXT, HttpStatus.SERVICE_UNAVAILABLE);
  }

  // ---------------------------------------------------------------- 账户管理

  /** `GET /api/admin/users`：只下发白名单字段，绝不返回 salt/hash。 */
  listUsers(): Record<string, unknown> {
    const web = this.config.web as { users?: Record<string, UserRecord> };
    const users = Object.entries(web.users ?? {}).map(([username, record]) => ({
      username,
      role: asText(record?.role) || 'user',
      enabled: record?.enabled === undefined ? true : Boolean(record.enabled),
      quota: this.quotaOf(record),
      used: this.videosUsed(username),
      // Python 该接口没有 expires；需求要求展示到期时间，这里补一个字段
      expires: asText(record?.expires),
    }));
    return { ok: true, users };
  }

  private quotaOf(record: UserRecord | undefined): number {
    const text = asText(record?.quota).trim();
    if (!/^-?\d+$/.test(text)) return 0;
    return Math.max(0, Number(text));
  }

  /** `POST /api/admin/users`：新增账户（201）。 */
  async addUser(body: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    const username = asText(body.username).trim();
    const password = asText(body.password);
    const role = asText(body.role) || 'user';
    if (!USERNAME_RE.test(username)) {
      throw adminError('账号请使用 2–32 位字母、数字、下划线、点或短横线。');
    }
    const users = this.userTable();
    if (Object.prototype.hasOwnProperty.call(users, username)) {
      throw adminError('账号已存在。', HttpStatus.CONFLICT);
    }
    if (password.length < 8) throw adminError('密码至少需要 8 位。');
    if (role !== 'user' && role !== 'admin') throw adminError('用户角色无效。');

    const hash = await makePassword(password);
    users[username] = { ...hash, role, enabled: true, version: 1, expires: '' };
    this.persistWeb();
    return { ok: true, username };
  }

  /** `POST /api/admin/users/<u>/reset`：重置密码并 bump version（踢掉旧会话）。 */
  async resetPassword(username: string, body: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    const user = this.userTable()[username];
    const password = asText(body.password);
    if (!user) throw adminError('账号不存在。', HttpStatus.NOT_FOUND);
    if (password.length < 8) throw adminError('新密码至少需要 8 位。');
    const hash = await makePassword(password);
    user.salt = hash.salt;
    user.hash = hash.hash;
    user.rounds = hash.rounds;
    user.version = Math.trunc(Number(user.version ?? 1)) + 1;
    this.persistWeb();
    return { ok: true };
  }

  /** `POST /api/admin/users/<u>/quota`：只改 quota，不 bump version（不把人踢下线）。 */
  setQuota(username: string, body: Record<string, unknown> = {}): Record<string, unknown> {
    const user = this.userTable()[username];
    if (!user) throw adminError('账号不存在。', HttpStatus.NOT_FOUND);
    const quota = toIntStrict(body.quota);
    if (quota === null) throw adminError('上限必须是 0 以上的整数（0 表示不限）。');
    if (quota < 0 || quota > 1_000_000) {
      throw adminError('上限必须是 0–1000000 的整数（0 表示不限）。');
    }
    user.quota = quota;
    this.persistWeb();
    const used = this.videosUsed(username);
    return {
      ok: true,
      username,
      quota,
      used,
      note: quota ? `该账户已用 ${used} 条，新上限即时生效。` : `该账户已用 ${used} 条，已改为不限。`,
    };
  }

  /** `POST /api/admin/users/<u>/disable`：停用（不能停自己、不能干掉最后一个管理员）。 */
  disableUser(username: string, actor: { username: string }): Record<string, unknown> {
    const users = this.userTable();
    const user = users[username];
    if (!user) throw adminError('账号不存在。', HttpStatus.NOT_FOUND);
    if (username === actor.username) {
      throw adminError('不能停用当前登录的管理员账号。', HttpStatus.CONFLICT);
    }
    if (asText(user.role) === 'admin' && user.enabled !== false) {
      const admins = Object.values(users).filter((item) => asText(item.role) === 'admin' && item.enabled !== false);
      if (admins.length <= 1) throw adminError('至少保留一个可用管理员。', HttpStatus.CONFLICT);
    }
    user.enabled = false;
    user.version = Math.trunc(Number(user.version ?? 1)) + 1;
    this.persistWeb();
    return { ok: true };
  }

  /** `POST /api/admin/users/<u>/enable`。 */
  enableUser(username: string): Record<string, unknown> {
    const user = this.userTable()[username];
    if (!user) throw adminError('账号不存在。', HttpStatus.NOT_FOUND);
    user.enabled = true;
    user.version = Math.trunc(Number(user.version ?? 1)) + 1;
    this.persistWeb();
    return { ok: true };
  }

  /** `DELETE /api/admin/users/<u>`：彻底删除（该账号会话立即失效）；不能删自己。 */
  deleteUser(username: string, actor: { username: string }): Record<string, unknown> {
    const users = this.userTable();
    if (!Object.prototype.hasOwnProperty.call(users, username)) {
      throw adminError('账号不存在。', HttpStatus.NOT_FOUND);
    }
    if (username === actor.username) {
      throw adminError('不能删除当前登录的账号。', HttpStatus.CONFLICT);
    }
    delete users[username];
    this.persistWeb();
    return { ok: true, username };
  }

  // ---------------------------------------------------------------- 内部

  /**
   * users 表：真实 web.json 是扁平结构。这里拿到的是 `config.web.users`
   * 同一个对象，改完直接整体原子落盘，其它配置项原样保留。
   */
  private userTable(): Record<string, UserRecord> {
    const web = this.config.web as { users?: Record<string, UserRecord> };
    if (!web.users || typeof web.users !== 'object') web.users = {};
    return web.users;
  }

  private persistWeb(): void {
    writeJsonAtomic(this.config.webFile, this.config.web);
  }

  private freeBytes(): number {
    try {
      const stat = fs.statfsSync(this.config.dataDir);
      return Number(stat.bavail) * Number(stat.bsize);
    } catch {
      return 0;
    }
  }
}
