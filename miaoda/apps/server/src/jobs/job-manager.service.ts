import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { AppConfigService } from '../config/app-config.service';
import { UploadsService } from '../uploads/uploads.service';
import { TemplateCatalogService } from '../templates/template-catalog.service';
import { WanClientService } from '../wan/wan-client.service';
import { JobRunner, needsRecheck, type JobRecord, type RunnerJob } from './job-runner';

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'canceled' | 'interrupted';

export interface JobImageSlot {
  upload_id: string;
  name: string;
  path: string;
}

export interface Job {
  id: string;
  owner: string;
  status: JobStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  batch_id: string;
  image_path: string;
  image_name: string;
  images: JobImageSlot[];
  image_upload_id: string;
  prompt_id: string;
  prompt: string;
  templates: Array<{ id: string; url?: string; key?: string; duration?: number | null; name?: string }>;
  template_ids: string[];
  resolution: string;
  duration: number;
  total: number;
  done: number;
  records: JobRecord[];
  summary: JobSummary | null;
  message: string;
  out_dir: string;
  log_tail: string[];
  cancel_requested: boolean;
  batch_ref: string;
  tags: Record<string, string>;
  style_no: string;
  lane: 'small' | 'big';
  /** 复查进度（只查询、不重新提交）；没有复查过时为空。 */
  recheck?: RecheckState | null;
}

export interface RecheckState {
  running: boolean;
  auto: boolean;
  total: number;
  checked: number;
  success: number;
  failed: number;
  still_running: number;
  errors: number;
  started_at: number;
  finished_at: number | null;
  message: string;
}

export interface JobSummary {
  total: number;
  success: number;
  failed: number;
  invalid: number;
  expired: number;
  canceled: number;
  timeout: number;
  unknown: number;
  skipped: number;
  running: number;
  pending: number;
  unsuccessful: number;
  billable_seconds: number;
  estimated_cost: number;
  cost: number;
  out_dir?: string;
}

const TERMINAL: JobStatus[] = ['succeeded', 'partial', 'failed', 'canceled', 'interrupted'];
const MONEY_KEYS = new Set(['billable_seconds', 'estimated_cost', 'cost']);
const LOG_TAIL_LINES = 40;

@Injectable()
export class JobManagerService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(JobManagerService.name);
  private readonly jobs = new Map<string, Job>();
  private readonly running = new Map<string, { job: Job; stop: () => void }>();
  private stopped = false;
  private timer: NodeJS.Timeout | null = null;

  constructor(
    private readonly config: AppConfigService,
    private readonly uploads: UploadsService,
    private readonly catalog: TemplateCatalogService,
    private readonly wan: WanClientService,
  ) {}

  get jobsDir(): string {
    return path.join(this.config.dataDir, 'jobs');
  }

  get logsDir(): string {
    return path.join(this.config.dataDir, 'logs');
  }

  get outputRoot(): string {
    return path.join(this.config.dataDir, 'output');
  }

  onModuleInit(): void {
    fs.mkdirSync(this.jobsDir, { recursive: true });
    fs.mkdirSync(this.logsDir, { recursive: true });
    fs.mkdirSync(this.outputRoot, { recursive: true });
    this.load();
    // 重启前正在跑的任务：云端可能仍在计费，标 interrupted 让人工核对，不自动重跑。
    for (const job of this.jobs.values()) {
      if (job.status === 'running') {
        job.status = 'interrupted';
        job.finished_at = Date.now() / 1000;
        job.message = '服务重启，任务已中断；已提交的条目请到控制台核对后再决定是否重试。';
        this.persist(job);
      }
    }
    this.timer = setInterval(() => this.dispatch(), 1000);
  }

  onModuleDestroy(): void {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    for (const slot of this.running.values()) slot.stop();
  }

  // ---------------------------------------------------------------- 对外接口

  create(params: {
    owner: string;
    slots: JobImageSlot[];
    templates: Job['templates'];
    promptId: string;
    prompt: string;
    resolution: string;
    duration: number;
    tags: Record<string, string>;
    styleNo: string;
    batchRef?: string;
  }): Job {
    const { slots } = params;
    if (!slots.length) throw new Error('请先上传服装图片。');
    if (!params.templates.length) throw new Error('至少选择一个视频场景');
    if (params.templates.length > this.config.maxJobItems) {
      throw new Error(`一次最多生成 ${this.config.maxJobItems} 条`);
    }
    if (!['480P', '720P'].includes(params.resolution)) {
      throw new Error('生成参数无效，请刷新页面后重试');
    }
    if (!params.prompt.trim()) throw new Error('提示词不能为空');
    if (params.prompt.length > 4000) throw new Error('提示词最多 4000 个字符');
    if (!params.tags.top) throw new Error('请填写「上衣」款号（云端归档路径按它命名）');

    const id = crypto.randomBytes(6).toString('hex');
    const now = Date.now() / 1000;
    const safeOwner = params.owner.replace(/[^A-Za-z0-9_-]+/g, '-').slice(0, 20) || 'user';
    const stamp = new Date();
    const batchId =
      `WEB-${safeOwner}-` +
      `${stamp.getFullYear()}${pad(stamp.getMonth() + 1)}${pad(stamp.getDate())}-` +
      `${pad(stamp.getHours())}${pad(stamp.getMinutes())}${pad(stamp.getSeconds())}-${id.slice(0, 4)}`;
    const job: Job = {
      id,
      owner: params.owner,
      status: 'queued',
      created_at: now,
      started_at: null,
      finished_at: null,
      batch_id: batchId,
      image_path: slots[0].path,
      image_name: slots[0].name,
      images: slots,
      image_upload_id: slots[0].upload_id,
      prompt_id: params.promptId,
      prompt: params.prompt.trim(),
      templates: params.templates,
      template_ids: params.templates.map((tpl) => tpl.id),
      resolution: params.resolution,
      duration: params.duration,
      total: params.templates.length,
      done: 0,
      records: [],
      summary: null,
      message: '已排队，开始前可取消。',
      out_dir: '',
      log_tail: [],
      cancel_requested: false,
      batch_ref: params.batchRef || '',
      tags: params.tags,
      style_no: params.styleNo,
      lane: params.templates.length <= this.config.smallLaneItems ? 'small' : 'big',
    };
    const queued = Array.from(this.jobs.values()).filter((item) => item.status === 'queued').length;
    if (queued >= 50) throw new Error('当前排队任务较多，请稍后再提交');
    this.jobs.set(id, job);
    this.persist(job);
    // 与 Python 一致：创建即返回 `queued`，由调度循环（1s 心跳）挑走，不在这里同步起跑。
    return job;
  }

  get(jobId: string): Job | undefined {
    return this.jobs.get(jobId);
  }

  list(owner: string, admin: boolean, ownerFilter = ''): Job[] {
    return Array.from(this.jobs.values())
      .filter((job) => (admin ? !ownerFilter || job.owner === ownerFilter : job.owner === owner))
      .sort((a, b) => b.created_at - a.created_at);
  }

  cancel(jobId: string): boolean {
    const job = this.jobs.get(jobId);
    if (!job) return false;
    if (job.status === 'queued') {
      job.status = 'canceled';
      job.message = '已取消。';
      job.finished_at = Date.now() / 1000;
      this.persist(job);
      return true;
    }
    if (job.status === 'running') {
      job.cancel_requested = true;
      job.message = '正在停止…';
      this.persist(job);
      this.running.get(jobId)?.stop();
      return true;
    }
    return false;
  }

  /** 重试失败项：只挑可重试状态，且不碰「云端已出片、只是下载失败」的条目。 */
  retryFailed(jobId: string, owner: string, admin: boolean): Job {
    const source = this.jobs.get(jobId);
    if (!source) throw new Error('找不到该任务');
    if (source.owner !== owner && !admin) throw new Error('无权操作该任务');
    if (!TERMINAL.includes(source.status)) throw new Error('任务尚未结束，暂时不能重试');
    const retryable = new Set(['failed', 'invalid', 'expired', 'canceled', 'skipped', 'timeout']);
    const ids = new Set(source.records.filter((rec) => retryable.has(rec.status)).map((rec) => rec.id));
    if (source.records.some((rec) => rec.download_failed)) {
      throw new Error(
        '其中存在「云端已出片、只是下载失败」的条目：重试会重新提交同一条模板、二次计费。请先点「复查补下载」。',
      );
    }
    if (source.records.some((rec) => rec.status === 'unknown')) {
      throw new Error('有提交结果未知的条目（云端可能已计费），请先到控制台核对，确认没有重复后再新建任务。');
    }
    const templates = source.templates.filter((tpl) => ids.has(tpl.id));
    if (!templates.length) throw new Error('该任务没有可重试的失败项');
    const slots = source.images.filter((slot) => fs.existsSync(slot.path));
    if (!slots.length) throw new Error('原任务没有可用的图片，请重新上传后新建任务');
    return this.create({
      owner: source.owner,
      slots,
      templates,
      promptId: source.prompt_id,
      prompt: source.prompt,
      resolution: source.resolution,
      duration: source.duration,
      tags: source.tags,
      styleNo: source.style_no,
      batchRef: source.batch_ref,
    });
  }

  /**
   * 复查超时 / 下载失败的条目：只按原 taskId 查云端，不重新提交。
   * 立即返回 job 快照（202），后台跑；前端轮询 `job.recheck` 看进度。
   */
  recheckTimeouts(jobId: string, auto = false): Job {
    const job = this.jobs.get(jobId);
    if (!job) throw new Error('找不到该任务');
    if (job.status === 'queued' || job.status === 'running') throw new Error('任务还在跑，跑完再复查');
    if (job.recheck?.running) throw new Error('上一次复查还没结束，请稍等');
    const targets = job.records.filter((rec) => needsRecheck(rec));
    if (!targets.length) throw new Error('没有等待超时或下载失败的条目需要复查');
    job.recheck = {
      running: true,
      auto,
      total: targets.length,
      checked: 0,
      success: 0,
      failed: 0,
      still_running: 0,
      errors: 0,
      started_at: Date.now() / 1000,
      finished_at: null,
      message: `正在向云端复查 ${targets.length} 条超时/下载失败任务（只查询，不重新提交）…`,
    };
    this.persist(job);
    void this.runRecheck(jobId);
    return job;
  }

  /** 后台复查：逐条查云端，边查边把记录与进度写回 job。 */
  private async runRecheck(jobId: string): Promise<void> {
    const job = this.jobs.get(jobId);
    if (!job) return;
    const targets = job.records.filter((rec) => needsRecheck(rec));
    this.appendLog(job, `复查超时/下载失败任务：${targets.length} 条，按原 taskId 向云端查询`);
    const runner = new JobRunner(
      {
        id: job.id,
        owner: job.owner,
        mode: this.config.defaultMode || 'edit',
        prompt: job.prompt,
        images: job.images,
        templates: job.templates,
        resolution: job.resolution,
        duration: job.duration,
        out_dir: job.out_dir,
        batch_id: job.batch_id,
      },
      this.wan,
      this.config,
      {
        onRecord: () => undefined,
        onLog: (message) => this.appendLog(job, message),
        stopped: () => false,
      },
    );
    try {
      await runner.recheck(targets, (rec) => {
        const live = this.jobs.get(jobId);
        if (!live) return;
        const index = live.records.findIndex((item) => item.id === rec.id);
        if (index >= 0) live.records[index] = rec;
        const state = live.recheck || {
          running: true,
          auto: false,
          total: targets.length,
          checked: 0,
          success: 0,
          failed: 0,
          still_running: 0,
          errors: 0,
          started_at: Date.now() / 1000,
          finished_at: null,
          message: '',
        };
        state.checked += 1;
        if (rec.status === 'success') {
          state.success += 1;
        } else if (rec.status === 'timeout') {
          // 仍是 timeout 有三种原因：云端还在跑（正常等）、没有 taskId、查询报错
          const cloud = String(rec.cloud_status || '');
          if (rec.download_failed || cloud === 'no_task_id' || cloud.startsWith('error:')) state.errors += 1;
          else state.still_running += 1;
        } else if (String(rec.cloud_status || '').startsWith('error:')) {
          state.errors += 1;
        } else {
          state.failed += 1;
        }
        state.message =
          `已复查 ${state.checked}/${state.total} 条：补回 ${state.success}、` +
          `云端已失败 ${state.failed}、仍在跑 ${state.still_running}、出错 ${state.errors}`;
        live.recheck = state;
        this.persist(live);
      });
    } catch (err) {
      const live = this.jobs.get(jobId);
      if (live) {
        const state = live.recheck;
        if (state) {
          state.message = `复查中断：${(err as Error).name}: ${(err as Error).message}`;
          state.errors += 1;
        }
        this.persist(live);
      }
      this.appendLog(job, `复查超时任务失败：${(err as Error).message}`);
    } finally {
      const live = this.jobs.get(jobId);
      if (live) {
        this.refreshSummary(live);
        const state = live.recheck;
        if (state) {
          state.running = false;
          state.finished_at = Date.now() / 1000;
        }
        this.writeReport(live);
        this.persist(live);
        if (state?.success) {
          this.appendLog(job, `复查完成：${state.success} 条云端已出片，已补下载并转为成功`);
        }
      }
    }
  }

  /** 对外视图：只下发白名单字段，金额按角色裁剪（与 Python public() 同口径）。 */
  publicView(job: Job, admin: boolean): Record<string, unknown> {
    const safe: Record<string, unknown> = {
      id: job.id,
      owner: job.owner,
      status: job.status,
      created_at: job.created_at,
      started_at: job.started_at,
      finished_at: job.finished_at,
      batch_id: job.batch_id,
      batch_ref: job.batch_ref,
      image_name: job.image_name,
      prompt_id: job.prompt_id,
      template_ids: job.template_ids,
      resolution: job.resolution,
      duration: job.duration,
      total: job.total,
      done: job.done,
      message: job.message,
      log_tail: job.log_tail,
      cancel_requested: job.cancel_requested,
      lane: job.lane,
      style_no: job.style_no,
      tags: job.tags,
      recheck: job.recheck ?? null,
      image_urls: job.images.filter((slot) => slot.upload_id).map((slot) => `/api/uploads/${slot.upload_id}`),
      image_names: job.images.map((slot) => slot.name),
    };
    if (job.summary) {
      safe.summary = Object.fromEntries(
        Object.entries(job.summary).filter(([key]) => key !== 'out_dir' && (admin || !MONEY_KEYS.has(key))),
      );
    } else {
      safe.summary = null;
    }
    const recordKeys = [
      'id',
      'status',
      'seconds',
      'error',
      'task_id',
      'request_id',
      'billable_seconds',
      'estimated_cost',
      'verify',
      'cos_key',
      'download_failed',
    ].filter((key) => admin || !MONEY_KEYS.has(key));
    safe.records = job.records.map((rec) => {
      const view: Record<string, unknown> = {};
      for (const key of recordKeys) {
        const value = (rec as unknown as Record<string, unknown>)[key];
        if (value !== undefined) view[key] = value;
      }
      return view;
    });
    if (job.out_dir) {
      safe.download_all = `/api/jobs/${job.id}/files.zip`;
      safe.report_url = `/api/jobs/${job.id}/report.csv`;
      for (const rec of safe.records as Array<Record<string, unknown>>) {
        if (rec.status === 'success') rec.download_url = `/api/jobs/${job.id}/download/${rec.id}`;
      }
    }
    const queue = Array.from(this.jobs.values())
      .filter((item) => item.status === 'queued')
      .sort((a, b) => a.created_at - b.created_at);
    safe.queue_position = queue.findIndex((item) => item.id === job.id) + 1;
    return safe;
  }

  outputPath(jobId: string): string | null {
    const job = this.jobs.get(jobId);
    if (!job || !job.out_dir) return null;
    return job.out_dir;
  }

  logPath(jobId: string): string {
    return path.join(this.logsDir, `${jobId}.log`);
  }

  // ---------------------------------------------------------------- 调度

  private dispatch(): void {
    if (this.stopped) return;
    const bigActive = Array.from(this.running.values()).filter((item) => item.job.lane === 'big');
    const smallActive = Array.from(this.running.values()).filter((item) => item.job.lane === 'small');
    const queued = Array.from(this.jobs.values())
      .filter((job) => job.status === 'queued')
      .sort((a, b) => a.created_at - b.created_at);
    for (const job of queued) {
      const lane = job.lane;
      const pool = lane === 'small' ? smallActive : bigActive;
      const limit = lane === 'small' ? this.config.maxActiveSmallJobs : this.config.maxActiveJobs;
      const perUser = this.config.maxActivePerUser;
      if (pool.length >= limit) continue;
      if (pool.filter((item) => item.job.owner === job.owner).length >= perUser) continue;
      this.start(job);
      pool.push({ job, stop: () => this.running.get(job.id)?.stop() });
    }
  }

  private start(job: Job): void {
    job.status = 'running';
    job.started_at = Date.now() / 1000;
    job.message = '正在生成…';
    job.out_dir = this.uniqueOutDir(job.batch_id);
    fs.mkdirSync(job.out_dir, { recursive: true });
    this.persist(job);
    let stopFlag = false;
    this.running.set(job.id, { job, stop: () => (stopFlag = true) });

    const runnerJob: RunnerJob = {
      id: job.id,
      owner: job.owner,
      mode: this.config.defaultMode || 'edit',
      prompt: job.prompt,
      images: job.images,
      templates: job.templates,
      resolution: job.resolution,
      duration: job.duration,
      aspect_ratio: undefined,
      out_dir: job.out_dir,
      batch_id: job.batch_id,
    };
    const runner = new JobRunner(runnerJob, this.wan, this.config, {
      onRecord: (record) => {
        const index = job.records.findIndex((item) => item.id === record.id);
        if (index >= 0) job.records[index] = record;
        else job.records.push(record);
        const done = job.records.filter((rec) => isTerminalRecord(rec.status)).length;
        job.done = done;
        this.persist(job);
      },
      onLog: (message) => this.appendLog(job, message),
      stopped: () => stopFlag || this.stopped,
    });

    void runner
      .run()
      .catch((err) => {
        this.appendLog(job, `执行器异常：${(err as Error).message}`);
      })
      .finally(() => {
        this.running.delete(job.id);
        this.finish(job);
      });
  }

  private finish(job: Job): void {
    this.refreshSummary(job);
    job.finished_at = Date.now() / 1000;
    this.writeReport(job);
    this.persist(job);
    this.dispatch();
  }

  /** 由 records 重算 summary 与终态（复查改了记录状态后也要跑一次，partial 可能变 succeeded）。 */
  private refreshSummary(job: Job): void {
    const counts = countRecords(job.records);
    const summary: JobSummary = {
      ...counts,
      total: job.records.length,
      unsuccessful: Math.max(0, job.records.length - counts.success),
      billable_seconds: round2(sum(job.records.map((rec) => Number(rec.billable_seconds) || 0))),
      estimated_cost: round2(sum(job.records.map((rec) => Number(rec.estimated_cost) || 0))),
      cost: 0,
      out_dir: job.out_dir,
    };
    summary.cost = summary.estimated_cost;
    job.summary = summary;
    const total = Math.max(0, summary.total);
    job.done = Math.max(0, Math.min(total, total - counts.running));
    if (job.cancel_requested) {
      job.status = 'canceled';
      job.message = '已停止继续提交；已提交的任务已处理完成。';
    } else if (total && counts.success >= total) {
      job.status = 'succeeded';
      job.message = '全部完成。';
    } else if (counts.success > 0) {
      job.status = 'partial';
      job.message =
        `部分完成：成功 ${counts.success}/${total}，另有 ${total - counts.success} 条未成功` +
        '（失败/无效/超时/取消/跳过），请检查失败项。';
    } else {
      job.status = 'failed';
      job.message = '任务未能完成，请查看失败原因。';
    }
  }

  /**
   * 导出 `report.csv`（与 Python `_export_csv` 同表头、同 `utf-8-sig` 编码）。
   * 只在任务收尾时写一次；下载接口直接读这个文件，缺失就 404。
   */
  private writeReport(job: Job): void {
    if (!job.out_dir) return;
    const rows: string[][] = [
      [
        '模板编号',
        '状态',
        '模式',
        '耗时(秒)',
        '计费秒数',
        '输出时长(秒)',
        '分辨率',
        '预估费用',
        '文件大小(KB)',
        '实测时长(秒)',
        '错误',
        '对象Key',
        'request_id',
      ],
    ];
    for (const rec of [...job.records].sort((a, b) => a.id.localeCompare(b.id))) {
      const verify = (rec.verify || {}) as Record<string, unknown>;
      const usage = (rec.usage || {}) as Record<string, unknown>;
      rows.push([
        rec.id,
        String(rec.status ?? ''),
        String(rec.mode ?? ''),
        cell(rec.seconds),
        cell(rec.billable_seconds),
        cell(usage.output_video_duration),
        String(rec.resolution ?? ''),
        cell(rec.estimated_cost),
        cell(verify.size_kb),
        cell(verify.duration),
        String(rec.error ?? ''),
        String(rec.cos_key ?? ''),
        String(rec.request_id ?? ''),
      ]);
    }
    const text = '\uFEFF' + rows.map((row) => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
    try {
      fs.writeFileSync(path.join(job.out_dir, 'report.csv'), text, 'utf8');
    } catch (err) {
      this.logger.warn(`导出 report.csv 失败（${job.id}）：${(err as Error).message}`);
    }
  }

  private uniqueOutDir(batchId: string): string {
    const base = path.join(this.outputRoot, batchId.replace(/[\\/:*?"<>|]+/g, '_'));
    let candidate = base;
    let index = 2;
    while (fs.existsSync(candidate)) {
      candidate = `${base}-${index}`;
      index += 1;
    }
    return candidate;
  }

  // ---------------------------------------------------------------- 持久化

  private persist(job: Job): void {
    const file = path.join(this.jobsDir, `${job.id}.json`);
    const tmp = path.join(this.jobsDir, `.${job.id}.${process.pid}.tmp`);
    try {
      fs.writeFileSync(tmp, JSON.stringify(job, null, 2), 'utf8');
      fs.renameSync(tmp, file);
    } catch (err) {
      this.logger.error(`任务写盘失败 ${job.id}：${(err as Error).message}`);
    }
  }

  private appendLog(job: Job, message: string): void {
    const line = `[${new Date().toISOString().slice(11, 19)}] ${message}`;
    job.log_tail = [...job.log_tail, line].slice(-LOG_TAIL_LINES);
    try {
      fs.appendFileSync(this.logPath(job.id), `${line}\n`, 'utf8');
    } catch {
      // 日志写不进去不影响任务
    }
    this.persist(job);
  }

  private load(): void {
    let entries: string[] = [];
    try {
      entries = fs.readdirSync(this.jobsDir).filter((name) => name.endsWith('.json'));
    } catch {
      return;
    }
    for (const name of entries) {
      try {
        const raw = fs.readFileSync(path.join(this.jobsDir, name), 'utf8').replace(/^\uFEFF/, '');
        const job = JSON.parse(raw) as Job;
        if (!job?.id) continue;
        job.records = job.records || [];
        job.images = job.images?.length
          ? job.images
          : job.image_path
            ? [{ upload_id: job.image_upload_id || '', name: job.image_name || '', path: job.image_path }]
            : [];
        this.jobs.set(job.id, job);
      } catch (err) {
        this.logger.warn(`跳过损坏的任务文件 ${name}：${(err as Error).message}`);
      }
    }
    this.logger.log(`已载入 ${this.jobs.size} 个任务`);
  }
}

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/** CSV 单元格：空值留空，其余转字符串（金额/秒数保留原样）。 */
function cell(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

/** 与 Python `csv.writer` 的最小引号策略一致：含逗号/引号/换行才加引号。 */
function csvCell(text: string): string {
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

const TERMINAL_RECORD_STATES = new Set(['success', 'failed', 'invalid', 'expired', 'canceled', 'timeout', 'unknown', 'skipped']);

function isTerminalRecord(status: string): boolean {
  return TERMINAL_RECORD_STATES.has(status);
}

function countRecords(records: JobRecord[]): Omit<JobSummary, 'total' | 'unsuccessful' | 'billable_seconds' | 'estimated_cost' | 'cost' | 'out_dir'> {
  const counts = {
    success: 0,
    failed: 0,
    invalid: 0,
    expired: 0,
    canceled: 0,
    timeout: 0,
    unknown: 0,
    skipped: 0,
    running: 0,
    pending: 0,
  };
  for (const rec of records) {
    if (rec.status === 'running') counts.running += 1;
    else if (rec.status === 'pending') counts.pending += 1;
    else if (rec.status in counts) counts[rec.status as keyof typeof counts] += 1;
  }
  return counts;
}
