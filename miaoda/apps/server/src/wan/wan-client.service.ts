import { Injectable, Logger } from '@nestjs/common';
import { AppConfigService } from '../config/app-config.service';

/** 官方接口路径（与 app/wan_api.py 一致）。 */
export const SUBMIT_SUFFIX = '/services/aigc/video-generation/video-synthesis';
export const TASK_SUFFIX = '/tasks/';

export const RESOLUTIONS = ['480P', '720P', '1080P'] as const;
export const RATIOS = ['adaptive', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16'] as const;
export const MAX_IMAGES = 10;
export const MAX_PROMPT = 20000;

export type WanCategory =
  | 'auth'
  | 'quota'
  | 'rate'
  | 'invalid'
  | 'network'
  | 'server'
  | 'canceled'
  | 'expired'
  | 'timeout'
  | 'unknown';

export class WanError extends Error {
  readonly category: WanCategory;
  readonly code: string;
  readonly requestId: string;
  readonly httpStatus: number;
  readonly retryable: boolean;

  constructor(
    category: WanCategory,
    message: string,
    opts: { code?: string; requestId?: string; httpStatus?: number; retryable?: boolean } = {},
  ) {
    super(message);
    this.name = 'WanError';
    this.category = category;
    this.code = opts.code || '';
    this.requestId = opts.requestId || '';
    this.httpStatus = opts.httpStatus || 0;
    this.retryable = opts.retryable ?? false;
  }
}

export class WaitTimeout extends WanError {
  constructor(
    readonly taskId: string,
    timeout: number,
  ) {
    super('timeout', `等待超时：任务 ${taskId} 在 ${timeout} 秒内没有出片（云端可能仍在处理）。`);
  }
}

export class TaskExpired extends WanError {
  constructor(readonly taskId: string) {
    super('expired', `任务 ${taskId} 已过期（超过 24 小时），云端不再保留结果。`);
  }
}

export class TaskCanceled extends WanError {
  constructor(readonly taskId: string) {
    super('canceled', `任务 ${taskId} 已在云端取消。`);
  }
}

export class WaitStopped extends WanError {
  constructor(readonly taskId: string) {
    super(
      'unknown',
      `已停止等待任务 ${taskId}；云端那条任务可能仍在运行，结果未知，请勿直接重试。`,
    );
  }
}

export class SubmitUnknown extends WanError {
  constructor(detail: string) {
    super('unknown', `提交结果未知：${detail}（云端可能已创建任务并计费，请先到控制台核对）`);
  }
}

export type TaskStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'canceled' | 'unknown';

const DASHSCOPE_STATUS: Record<string, TaskStatus> = {
  PENDING: 'pending',
  RUNNING: 'running',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
  CANCELED: 'canceled',
  UNKNOWN: 'unknown',
};

export interface SubmitResult {
  taskId: string;
  status: TaskStatus;
  requestId: string;
  raw: unknown;
}

export interface TaskResult {
  taskId: string;
  status: TaskStatus;
  videoUrl: string;
  usage: Record<string, unknown>;
  code: string;
  message: string;
  requestId: string;
  raw: unknown;
}

/**
 * 最小间隔令牌桶：与 app/wan_api.py::_RateLimiter 同口径。
 * 二选一：rpm（每分钟）或 qps（每秒）。超出就等，自然拉长轮询。
 */
export class RateLimiter {
  static readonly PENALTY_CAP = 60;
  private readonly interval: number;
  private next = 0;

  constructor(opts: { qps?: number; rpm?: number }) {
    const rate = opts.rpm ? Math.max(1, opts.rpm) / 60 : Math.max(1, opts.qps ?? 10);
    this.interval = 1 / rate;
  }

  async acquire(): Promise<void> {
    const now = performance.now() / 1000;
    const wait = this.next - now;
    this.next = Math.max(now, this.next) + this.interval;
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait * 1000));
  }

  penalize(seconds = 5): void {
    const now = performance.now() / 1000;
    const target = Math.max(this.next, now) + Math.max(0, seconds);
    this.next = Math.min(target, now + RateLimiter.PENALTY_CAP);
  }
}

/** 计数信号量（在飞名额）。 */
export class Semaphore {
  private active = 0;
  private readonly waiters: Array<() => void> = [];

  constructor(private readonly limit: number) {}

  async run<T>(task: () => Promise<T>): Promise<T> {
    if (this.active >= this.limit) {
      await new Promise<void>((resolve) => this.waiters.push(resolve));
    }
    this.active += 1;
    try {
      return await task();
    } finally {
      this.active -= 1;
      const next = this.waiters.shift();
      if (next) next();
    }
  }
}

export interface WanTaskInput {
  prompt: string;
  imageUrls: string[];
  videoUrls: string[];
  resolution: string;
  duration: number;
  aspectRatio: string;
  mode: string;
}

@Injectable()
export class WanClientService {
  private readonly logger = new Logger(WanClientService.name);
  private readonly submitLimiter: RateLimiter;
  private readonly queryLimiter: RateLimiter;
  readonly inflight: Semaphore;
  readonly smallInflight: Semaphore;

  constructor(private readonly config: AppConfigService) {
    this.submitLimiter = new RateLimiter({ rpm: config.submitRpm });
    this.queryLimiter = new RateLimiter({ qps: config.queryQps });
    this.inflight = new Semaphore(Math.max(1, config.maxInflight));
    this.smallInflight = new Semaphore(Math.max(1, config.smallLaneInflight));
  }

  /** 内部任务 → 真正发给服务端的请求体（对应 to_wire_body）。 */
  wireBody(task: WanTaskInput): Record<string, unknown> {
    const media: Array<{ type: string; url: string }> = [];
    if (task.mode === 'edit') {
      for (const url of task.videoUrls) media.push({ type: 'reference_video', url });
      for (const url of task.imageUrls) media.push({ type: 'reference_image', url });
    } else {
      for (const url of task.imageUrls) media.push({ type: 'reference_image', url });
      for (const url of task.videoUrls) media.push({ type: 'reference_video', url });
    }
    return {
      model: this.config.model,
      input: { prompt: task.prompt, media },
      parameters: {
        resolution: task.resolution,
        ratio: task.aspectRatio,
        duration: task.duration,
        audio: false,
        prompt_extend: false,
        watermark: false,
      },
    };
  }

  /** 提交前校验（对应 validate_task 的核心约束，错误文案保持可读）。 */
  validate(input: WanTaskInput): string[] {
    const errors: string[] = [];
    if (!input.prompt || !input.prompt.trim()) errors.push('prompt 必填，且不能为空字符串');
    if (input.prompt && input.prompt.length > MAX_PROMPT) {
      errors.push(`prompt 长度 ${input.prompt.length}，超过官方上限 ${MAX_PROMPT} 字符`);
    }
    if (input.imageUrls.length > MAX_IMAGES) {
      errors.push(`imageUrls 最多 ${MAX_IMAGES} 张，当前 ${input.imageUrls.length} 张`);
    }
    if (input.mode === 'edit' && input.videoUrls.length === 0) {
      errors.push('mode=edit 需要至少一段参考视频（videoUrls）；只给图片请用 mode=reference');
    }
    if (!(RESOLUTIONS as readonly string[]).includes(input.resolution)) {
      errors.push(`resolution 只能是 ${RESOLUTIONS.join('/')}，收到 ${JSON.stringify(input.resolution)}`);
    }
    if (!(RATIOS as readonly string[]).includes(input.aspectRatio)) {
      errors.push(`aspectRatio 只能是 ${RATIOS.join('/')}，收到 ${JSON.stringify(input.aspectRatio)}`);
    }
    if (input.duration !== -1 && !(Number.isInteger(input.duration) && input.duration >= 2 && input.duration <= 30)) {
      errors.push(`duration 只能是 -1 或 2–30 的整数，收到 ${input.duration}`);
    }
    return errors;
  }

  async submit(body: Record<string, unknown>): Promise<SubmitResult> {
    await this.submitLimiter.acquire();
    const url = join(this.config.wanBaseUrl, SUBMIT_SUFFIX);
    const data = await this.requestJson('POST', url, body, { 'X-DashScope-Async': 'enable' }, 90);
    const output = (data.output || {}) as Record<string, unknown>;
    const taskId = String(output.task_id || '').trim();
    if (!taskId) {
      const code = String(data.code || '').trim();
      const message = String(data.message || '提交未返回 task_id').trim();
      throw this.classifyError(code, message, 200, String(data.request_id || ''));
    }
    return {
      taskId,
      status: DASHSCOPE_STATUS[String(output.task_status || '').toUpperCase()] || 'pending',
      requestId: String(data.request_id || ''),
      raw: data,
    };
  }

  async getTask(taskId: string): Promise<TaskResult> {
    await this.queryLimiter.acquire();
    const url = join(this.config.wanBaseUrl, `${TASK_SUFFIX}${encodeURIComponent(taskId)}`);
    const data = await this.requestJson('GET', url, null, {}, 45);
    const output = (data.output || {}) as Record<string, unknown>;
    return {
      taskId: String(output.task_id || taskId),
      status: DASHSCOPE_STATUS[String(output.task_status || '').toUpperCase()] || 'unknown',
      videoUrl: String(output.video_url || ''),
      usage: (data.usage || {}) as Record<string, unknown>,
      code: String(output.code || data.code || ''),
      message: String(output.message || data.message || ''),
      requestId: String(data.request_id || ''),
      raw: data,
    };
  }

  async download(url: string, dest: string): Promise<void> {
    let lastError: unknown = null;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        const response = await fetch(url, { signal: AbortSignal.timeout(600_000) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const buffer = Buffer.from(await response.arrayBuffer());
        if (buffer.length === 0) throw new Error('成片为空');
        await writeFileAtomic(dest, buffer);
        return;
      } catch (err) {
        lastError = err;
        this.logger.warn(`下载失败（第 ${attempt}/3 次）：${(err as Error).message}`);
        if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, 2000 * attempt));
      }
    }
    throw lastError instanceof Error ? lastError : new Error(String(lastError));
  }

  private classifyError(code: string, message: string, status: number, requestId = ''): WanError {
    const text = `${code} ${message}`;
    if (status === 401 || status === 403 || /InvalidApiKey|Unauthorized/i.test(text)) {
      return new WanError('auth', `API Key 无效或无权限：${message || code}`, { code, requestId, httpStatus: status });
    }
    if (status === 429 || /Throttling|TooManyRequests|rate.?limit/i.test(text)) {
      this.submitLimiter.penalize(5);
      this.queryLimiter.penalize(5);
      return new WanError('rate', `触发官方限流：${message || code}`, { code, requestId, httpStatus: status, retryable: true });
    }
    if (/quota|insufficient|Arrearage/i.test(text)) {
      return new WanError('quota', `账户额度不足：${message || code}`, { code, requestId, httpStatus: status });
    }
    if (status >= 500 || /InternalError|ServiceUnavailable/i.test(text)) {
      return new WanError('server', `服务端错误：${message || code}`, { code, requestId, httpStatus: status, retryable: true });
    }
    if (status >= 400) {
      return new WanError('invalid', `请求被拒绝：${message || code}`, { code, requestId, httpStatus: status });
    }
    return new WanError('unknown', message || code || '未知错误', { code, requestId, httpStatus: status });
  }

  private async requestJson(
    method: string,
    url: string,
    payload: Record<string, unknown> | null,
    headers: Record<string, string>,
    timeoutSeconds: number,
  ): Promise<Record<string, unknown>> {
    const apiKey = this.config.apiKey;
    if (!apiKey) throw new WanError('auth', '服务端没有配置 wan.api_key');
    const init: RequestInit = {
      method,
      headers: { Authorization: `Bearer ${apiKey}`, ...headers },
      signal: AbortSignal.timeout(timeoutSeconds * 1000),
    };
    if (payload !== null) {
      init.headers = { ...(init.headers as Record<string, string>), 'Content-Type': 'application/json' };
      init.body = JSON.stringify(payload);
    }
    let response: Response;
    try {
      response = await fetch(url, init);
    } catch (err) {
      const text = (err as Error).message || String(err);
      if (/abort|timeout/i.test(text)) {
        throw new WanError('network', `请求超时（${timeoutSeconds}s）：${url}`);
      }
      throw new WanError('network', `网络错误：${text}`, { retryable: true });
    }
    const raw = await response.text();
    let data: Record<string, unknown> = {};
    try {
      data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    } catch {
      data = { message: raw.slice(0, 300) };
    }
    if (!response.ok) {
      throw this.classifyError(
        String(data.code || ''),
        String(data.message || raw.slice(0, 300)),
        response.status,
        String(data.request_id || ''),
      );
    }
    return data;
  }
}

function join(base: string, suffix: string): string {
  return `${base.replace(/\/+$/, '')}/${suffix.replace(/^\/+/, '')}`;
}

async function writeFileAtomic(dest: string, buffer: Buffer): Promise<void> {
  const fs = await import('node:fs/promises');
  const path = await import('node:path');
  await fs.mkdir(path.dirname(dest), { recursive: true });
  const tmp = path.join(path.dirname(dest), `.${path.basename(dest)}.${process.pid}.tmp`);
  await fs.writeFile(tmp, buffer);
  await fs.rename(tmp, dest);
}
