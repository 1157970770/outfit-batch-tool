import { Logger } from '@nestjs/common';
import * as fs from 'node:fs';
import * as fsp from 'node:fs/promises';
import * as path from 'node:path';
import { AppConfigService } from '../config/app-config.service';
import { StorageService } from '../storage/storage.service';
import {
  MAX_IMAGES,
  SubmitUnknown,
  TaskCanceled,
  TaskExpired,
  WaitStopped,
  WaitTimeout,
  WanClientService,
  WanError,
  type WanTaskInput,
} from '../wan/wan-client.service';

export const MAX_BASE64_TOTAL_MB = 40;

export type RecordStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'invalid'
  | 'timeout'
  | 'expired'
  | 'canceled'
  | 'unknown'
  | 'skipped';

export interface JobRecord {
  id: string;
  template: string;
  status: RecordStatus;
  provider: string;
  model: string;
  mode: string;
  resolution: string;
  file: string;
  billable_seconds: number | null;
  estimated_cost: number | null;
  cost: number | null;
  seconds: number | null;
  error: string | null;
  task_id?: string;
  request_id?: string;
  usage?: Record<string, unknown>;
  submitted_at?: string;
  task_expires_at?: number;
  download_failed?: boolean;
  stopped?: boolean;
  resumed?: boolean;
  verify?: Record<string, unknown>;
  verify_error?: string;
  /** 归档到 COS 后的对象 Key（启用 COS 时填）。 */
  cos_key?: string;
  cos_url?: string;
  archive_error?: string;
  /** 复查（只查询、不重新提交）时记下的云端状态与时间。 */
  checked_at?: string;
  cloud_status?: string;
}

/** 该走「复查补下载」而不是「重试」：云端可能已出片并计费，重提 = 二次计费。 */
export function needsRecheck(rec: JobRecord | undefined | null): boolean {
  if (!rec) return false;
  return rec.status === 'timeout' || Boolean(rec.download_failed);
}

export interface RunnerJob {
  id: string;
  owner: string;
  mode: string;
  prompt: string;
  images: Array<{ upload_id: string; name: string; path: string }>;
  templates: Array<{ id: string; url?: string; key?: string; duration?: number | null }>;
  resolution: string;
  duration: number;
  aspect_ratio?: string;
  out_dir: string;
  batch_id: string;
  /** 归档路径里的款号段取「上衣」，老数据退回 style_no。 */
  tags?: Record<string, string>;
  style_no?: string;
}

export interface JobRunnerHooks {
  onRecord(record: JobRecord): void;
  onLog(message: string): void;
  stopped(): boolean;
}

/**
 * 一条 job 的执行器：逐条「提交 → 轮询 → 下载」，语义对齐 app/core.py::BatchRunner。
 * 与 Python 版的差异只有一处：图片一律内联 Base64（COS 直传在 P4 补齐），
 * 因此受 40MB 总量闸门约束（未启用 COS 时的口径）。
 */
export class JobRunner {
  private readonly logger = new Logger(JobRunner.name);
  private imageUrls: string[] | null = null;

  constructor(
    private readonly job: RunnerJob,
    private readonly wan: WanClientService,
    private readonly config: AppConfigService,
    private readonly hooks: JobRunnerHooks,
    private readonly storage?: StorageService,
  ) {}

  async run(): Promise<void> {
    const started = Date.now();
    this.hooks.onLog(
      `开始生成 ${this.job.templates.length} 条视频（${this.job.resolution}，${this.job.duration}s，并发 ${this.config.maxConcurrency}）`,
    );
    try {
      await this.ensureImages();
    } catch (err) {
      const message = (err as Error).message;
      this.hooks.onLog(`图片准备失败：${message}`);
      for (const tpl of this.job.templates) {
        this.hooks.onRecord({
          id: tpl.id,
          template: tpl.url || '',
          status: 'failed',
          provider: this.config.provider,
          model: this.config.model,
          mode: this.job.mode,
          resolution: this.job.resolution,
          file: path.join(this.job.out_dir, `${tpl.id}.mp4`),
          billable_seconds: null,
          estimated_cost: null,
          cost: null,
          seconds: null,
          error: message,
        });
      }
      this.hooks.onLog(`耗时 ${((Date.now() - started) / 1000).toFixed(1)}s`);
      return;
    }

    const queue = [...this.job.templates];
    const workers = Math.max(1, Math.min(this.config.maxConcurrency, queue.length));
    await Promise.all(
      Array.from({ length: workers }, async () => {
        for (;;) {
          const tpl = queue.shift();
          if (!tpl) return;
          const record = await this.runOne(tpl);
          this.hooks.onRecord(record);
        }
      }),
    );
    this.hooks.onLog(`全部结束，耗时 ${((Date.now() - started) / 1000).toFixed(1)}s`);
  }

  /** 多图按槽位顺序转 data URI；总量超 40MB 直接整批失败（少一张会让「图N」错位）。 */
  private async ensureImages(): Promise<void> {
    if (this.imageUrls) return;
    if (!this.job.images.length) throw new Error('缺少定妆图');
    if (this.job.images.length > MAX_IMAGES) {
      throw new Error(`图片最多 ${MAX_IMAGES} 张（1 张定妆图 + 9 张细节图）。`);
    }
    let totalMb = 0;
    for (const slot of this.job.images) {
      if (!fs.existsSync(slot.path)) throw new Error(`图片已丢失：${slot.name || slot.path}`);
      totalMb += fs.statSync(slot.path).size / 1024 / 1024;
    }
    if (totalMb > MAX_BASE64_TOTAL_MB) {
      throw new Error(
        `未启用 COS 时图片以 Base64 随每次提交发送，图片总体积需 ≤ ${MAX_BASE64_TOTAL_MB}MB` +
          `（当前 ${totalMb.toFixed(1)}MB）；请减少细节图或联系管理员启用 COS。`,
      );
    }
    const urls: string[] = [];
    for (const [index, slot] of this.job.images.entries()) {
      const buffer = await fsp.readFile(slot.path);
      this.hooks.onLog(`准备第 ${index + 1}/${this.job.images.length} 张图片：${slot.name}`);
      urls.push(`data:${mimeOf(buffer)};base64,${buffer.toString('base64')}`);
    }
    this.imageUrls = urls;
    this.hooks.onLog(`${urls.length} 张图片已就绪（Base64 内联），${this.job.templates.length} 条任务共用`);
  }

  private async runOne(tpl: RunnerJob['templates'][number]): Promise<JobRecord> {
    const outFile = path.join(this.job.out_dir, `${tpl.id}.mp4`);
    let record: JobRecord = {
      id: tpl.id,
      template: tpl.url || '',
      status: 'pending',
      provider: this.config.provider,
      model: this.config.model,
      mode: this.job.mode,
      resolution: this.job.resolution,
      file: outFile,
      billable_seconds: null,
      estimated_cost: null,
      cost: null,
      seconds: null,
      error: null,
    };
    if (this.hooks.stopped()) {
      record.status = 'skipped';
      return record;
    }
    if (!tpl.url) {
      record.status = 'invalid';
      record.error = '所选视频尚未上传到云端模板库，无法提交生成。';
      return record;
    }

    const input: WanTaskInput = {
      prompt: this.job.prompt,
      imageUrls: this.imageUrls || [],
      videoUrls: [tpl.url],
      resolution: this.job.resolution,
      duration: this.job.duration,
      aspectRatio: this.job.aspect_ratio || this.config.defaultRatio,
      mode: this.job.mode,
    };
    const errors = this.wan.validate(input);
    if (errors.length) {
      record.status = 'invalid';
      record.error = errors.join('; ');
      return record;
    }

    const started = Date.now();
    const semaphore = this.job.templates.length <= this.config.smallLaneItems ? this.wan.smallInflight : this.wan.inflight;
    try {
      const wire = this.wan.wireBody(input);
      const final = await semaphore.run(() => this.submitAndWait(wire, record));
      if (!final) return record;
      if (final.status === 'unknown') throw new TaskExpired(final.taskId);
      if (final.status === 'canceled') throw new TaskCanceled(final.taskId);
      if (final.status === 'failed') {
        throw new WanError(
          'invalid',
          `任务失败 [${final.code || 'UNKNOWN'}] ${final.message || '无错误信息'}` +
            (final.requestId ? ` request_id=${final.requestId}` : ''),
        );
      }
      if (final.status !== 'succeeded' || !final.videoUrl) {
        throw new WanError('unknown', `任务状态 ${final.status} 但没有结果地址`);
      }

      const usage = final.usage || {};
      const billable = usageSeconds(usage);
      const price = Number(this.config.unitPrice(this.job.resolution) || 0);
      record.request_id = final.requestId || record.request_id || '';
      record.usage = usage;
      record.billable_seconds = billable;
      record.estimated_cost = price ? Math.round(billable * price * 10000) / 10000 : null;
      record.cost = record.estimated_cost;
      record.seconds = Math.round((Date.now() - started) / 100) / 10;

      try {
        await this.wan.download(final.videoUrl, outFile);
        record.status = 'success';
        record.verify = await verifyFile(outFile, tpl.duration);
        await this.archive(record, outFile);
      } catch (err) {
        // 记账已在下载之前完成：下载失败仍然计费，这里只标记并保留金额。
        record.status = 'failed';
        record.download_failed = true;
        record.error =
          `云端已出片并按 ${billable} 秒计费，但下载失败：${(err as Error).name}: ${(err as Error).message}`;
      }
    } catch (err) {
      record = applyError(record, err);
    }
    return record;
  }

  /**
   * 归档到 COS：`{prefix}/output/{日期}[/{款号}]/{批次}/{文件名}-{指纹8}.{ext}`。
   *
   * 归档失败**不算任务失败**——视频已经在本机了，记 `archive_error`，下轮补传即可
   * （与 Python `BatchRunner._archive` 同口径）。
   */
  private async archive(record: JobRecord, file: string): Promise<void> {
    const storage = this.storage;
    if (!storage || !storage.enabled) return;
    const tags = (this.job.tags || {}) as Record<string, string>;
    const style = String(tags.top || this.job.style_no || '').trim();
    try {
      const key = storage.objectKey('output', path.basename(file), {
        batch: this.job.batch_id,
        prefix: storage.cleanPrefix,
        digest: storage.fingerprint(file),
        style,
      });
      await storage.putFile(file, key, 'video/mp4');
      record.cos_key = key;
      record.cos_url = storage.signUrl(key, 7 * 24 * 3600);
    } catch (err) {
      record.archive_error = `${(err as Error).name}: ${(err as Error).message}`.slice(0, 200);
      this.hooks.onLog(`归档失败（不影响成片）：${record.archive_error}`);
    }
  }

  /**
   * 复查「等待超时 / 下载失败」的条目：只按原 taskId 查云端，**绝不重新提交**。
   *
   * 为什么必须单独做：超时只说明本机没等到结果，云端往往已经出片并按秒计费，
   * 直接「重试」= 重新提交 = 二次计费。这里查完再决定补下载还是落终态。
   * 每条处理完回调一次 `onUpdate`，前端轮询 job 就能看到进度。
   */
  async recheck(
    records: JobRecord[],
    onUpdate: (rec: JobRecord) => void,
  ): Promise<JobRecord[]> {
    const durations = new Map(this.job.templates.map((tpl) => [String(tpl.id), tpl.duration ?? null]));
    const updated: JobRecord[] = [];
    for (const item of records) {
      const rec: JobRecord = { ...item };
      rec.checked_at = new Date().toISOString().slice(0, 19).replace('T', ' ');
      const taskId = String(rec.task_id || '').trim();
      if (!taskId) {
        rec.cloud_status = 'no_task_id';
        rec.error = '这条没有 taskId，无法向云端复查；只能重新提交（会二次计费）';
        updated.push(rec);
        onUpdate(rec);
        continue;
      }
      let final: Awaited<ReturnType<WanClientService['getTask']>>;
      try {
        final = await this.wan.getTask(taskId);
      } catch (err) {
        const category = err instanceof WanError ? err.category : 'unknown';
        rec.cloud_status = `error:${category}`;
        rec.error =
          `复查失败（${category}）：${(err as Error).message}。云端任务可能仍在运行，` +
          '稍后再点「复查超时任务」';
        updated.push(rec);
        onUpdate(rec);
        continue;
      }
      rec.cloud_status = final.status;
      rec.request_id = final.requestId || rec.request_id || '';
      if (final.status === 'succeeded' && final.videoUrl) {
        const usage = final.usage || {};
        const billable = usageSeconds(usage);
        const resolution = String(rec.resolution || this.job.resolution);
        const price = Number(this.config.unitPrice(resolution) || 0);
        rec.usage = usage;
        rec.billable_seconds = billable;
        rec.estimated_cost = price ? Math.round(billable * price * 10000) / 10000 : null;
        rec.cost = rec.estimated_cost;
        const outFile = rec.file || path.join(this.job.out_dir, `${rec.id}.mp4`);
        try {
          await this.wan.download(final.videoUrl, outFile);
          rec.file = outFile;
          rec.status = 'success';
          rec.download_failed = false;
          rec.error = null;
          rec.verify = await verifyFile(outFile, durations.get(String(rec.id)) ?? null);
          rec.task_expires_at = Math.round(Date.now() / 1000) + 24 * 3600;
        } catch (err) {
          rec.status = 'failed';
          rec.download_failed = true;
          rec.error =
            `云端已出片并按 ${billable} 秒计费，但补下载失败：` +
            `${(err as Error).name}: ${(err as Error).message}`;
        }
      } else if (final.status === 'failed' || final.status === 'canceled' || final.status === 'unknown') {
        rec.status = final.status === 'unknown' ? 'expired' : final.status;
        rec.download_failed = false;
        rec.error =
          `云端已判定：${final.status}` +
          (final.code ? ` [${final.code}]` : '') +
          (final.message ? ` ${final.message}` : '');
      } else {
        // 云端还在跑：保持 timeout，只更新 cloud_status，不占在飞额度。
        rec.status = 'timeout';
        rec.error =
          `云端仍在处理（${final.status}），本机没在时限内等到结果；` +
          '稍后再复查一次，不要重新提交（会二次计费）';
      }
      updated.push(rec);
      onUpdate(rec);
    }
    return updated;
  }

  private async submitAndWait(
    wire: Record<string, unknown>,
    record: JobRecord,
  ): Promise<{ status: string; taskId: string; videoUrl: string; usage: Record<string, unknown>; code: string; message: string; requestId: string } | null> {
    let submitted;
    try {
      submitted = await this.wan.submit(wire);
    } catch (err) {
      if (err instanceof WanError && err.category === 'network') {
        throw new SubmitUnknown((err as Error).message);
      }
      throw err;
    }
    record.task_id = submitted.taskId;
    record.request_id = submitted.requestId;
    record.submitted_at = new Date().toISOString().slice(0, 19).replace('T', ' ');
    record.status = 'running';
    this.hooks.onLog(`[${record.id}] 已提交 taskId=${submitted.taskId}`);

    const deadline = Date.now() + this.config.pollTimeoutMs;
    let interval = this.config.pollIntervalMs;
    for (;;) {
      if (this.hooks.stopped()) throw new WaitStopped(submitted.taskId);
      if (Date.now() > deadline) throw new WaitTimeout(submitted.taskId, Math.round(this.config.pollTimeoutMs / 1000));
      await new Promise((resolve) => setTimeout(resolve, interval));
      if (this.hooks.stopped()) throw new WaitStopped(submitted.taskId);
      const result = await this.wan.getTask(submitted.taskId);
      if (result.status === 'succeeded' || result.status === 'failed' || result.status === 'canceled' || result.status === 'unknown') {
        record.task_expires_at = Math.round(Date.now() / 1000) + 24 * 3600;
        return result;
      }
      // 轮询间隔按 1.5 倍退避，最长 60 秒（与 Python 的固定 15 秒相比更省额度）
      interval = Math.min(60_000, Math.round(interval * 1.5));
    }
  }
}

function applyError(record: JobRecord, err: unknown): JobRecord {
  if (err instanceof SubmitUnknown) {
    record.status = 'unknown';
    record.error = err.message;
    return record;
  }
  if (err instanceof WaitStopped) {
    record.status = 'unknown';
    record.stopped = true;
    record.error = err.message;
    return record;
  }
  if (err instanceof WaitTimeout) {
    record.status = 'timeout';
    record.error = err.message;
    return record;
  }
  if (err instanceof TaskExpired) {
    record.status = 'expired';
    record.error = err.message;
    return record;
  }
  if (err instanceof TaskCanceled) {
    record.status = 'canceled';
    record.error = err.message;
    return record;
  }
  if (err instanceof WanError) {
    record.status = 'failed';
    record.error = err.message;
    if (err.requestId) record.request_id = err.requestId;
    return record;
  }
  record.status = 'failed';
  record.error = `${(err as Error).name || 'Error'}: ${(err as Error).message}`;
  return record;
}

function usageSeconds(usage: Record<string, unknown>): number {
  const first = Number(usage.duration || 0);
  if (Number.isFinite(first) && first > 0) return Math.round(first * 100) / 100;
  const second = Number(usage.output_video_duration || 0);
  return Number.isFinite(second) && second > 0 ? Math.round(second * 100) / 100 : 0;
}

function mimeOf(buffer: Buffer): string {
  if (buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return 'image/png';
  }
  if (buffer[0] === 0xff && buffer[1] === 0xd8) return 'image/jpeg';
  if (buffer.subarray(0, 4).toString() === 'RIFF' && buffer.subarray(8, 12).toString() === 'WEBP') {
    return 'image/webp';
  }
  return 'application/octet-stream';
}

/** 尽力校验成片（有 ffprobe 才有结果，失败不影响任务状态）。 */
async function verifyFile(file: string, expectedSeconds?: number | null): Promise<Record<string, unknown>> {
  const stat = await fsp.stat(file);
  const info: Record<string, unknown> = {
    size_mb: Math.round((stat.size / 1024 / 1024) * 100) / 100,
    expected_seconds: expectedSeconds ?? null,
  };
  return info;
}
