/**
 * 生图任务管理：落盘 + 单进程内存队列 + N 个并发 worker + 重启恢复。
 * `app/imggen/manager.py` 的 TS 版。
 *
 * 已知债（与 Python 一致，别当 bug 修）：队列在进程内存里，没有跨进程锁，
 * 同一个 data_dir 不能起两个实例；重启不做断点续传，进行中的任务直接落成
 * 「生成失败」/「部分完成」，error 写「服务重启，任务中断，请重新提交。」。
 *
 * 数据目录与 Python 同名：`<dataDir>/imagegen/tasks/<id>.json`、`<dataDir>/imagegen/files/<id>/`。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomUUID } from 'node:crypto';

import { HttpException, Injectable, Logger } from '@nestjs/common';

import { AppConfigService } from '../config/app-config.service';
import { readJsonFile } from '../common/fs-json';
import * as gemini from './imagegen-gemini';
import {
  GEMINI_SIZE_BY_VALUE,
  IMAGE_MODEL_IDS,
  IMAGE_PROMPT_LIMIT,
  _as_int,
  _quality_pairs,
  _size_options,
  clamp_timeout,
  image_model_label,
  image_model_provider,
  image_model_spec,
  image_quality_options,
  image_request_parameters,
  is_gemini_model,
  max_count as clamp_max_count,
  max_inflight as clamp_max_inflight,
  max_references as clamp_max_references,
  supports_image_parameters,
} from './imagegen-models';
import {
  api_size_for_output,
  finalize_output_image,
  output_spec_label,
} from './imagegen-processing';
import { edit_image, explain_error, generate_image } from './imagegen-api';

export const STATUS_QUEUED = '排队中';
export const STATUS_RUNNING = '生成中';
export const STATUS_DONE = '已完成';
export const STATUS_PARTIAL = '部分完成';
export const STATUS_FAILED = '生成失败';

export const ACTIVE_STATUSES: ReadonlySet<string> = new Set([STATUS_QUEUED, STATUS_RUNNING]);

/** `server/imagegen.json` 的默认值（与 Python `DEFAULT_CONFIG` 逐字一致）。 */
export const DEFAULT_IMAGE_CONFIG = {
  enabled: true,
  base_url: 'https://api2.laozhang.ai/v1/',
  api_key: '',
  gemini_base_url: '',
  model: 'gpt-image-2-vip',
  default_quality: 'high',
  timeout_seconds: 360,
  max_count: 4,
  max_references: 10,
  max_inflight: 3,
  price_per_image: '0.03',
} as const;

export interface ImagegenUser {
  username?: string;
  role?: string;
}

/** 建任务时前端已经把参考图上传好并给了 upload_id；这里只收已解析的槽位。 */
export interface ImageReferenceSlot {
  upload_id?: string;
  name?: string;
  path: string;
}

export interface ImageTaskReference {
  index: number;
  name: string;
  upload_id: string;
  file: string;
}

export interface ImageTaskImage {
  index: number;
  file: string;
  name: string;
  width: number;
  height: number;
  size_mb: number;
  created_at: number;
}

export interface ImageTaskRecord {
  id: string;
  owner: string;
  created_at: number;
  updated_at: number;
  started_at: number;
  finished_at: number;
  elapsed_ms: number;
  prompt: string;
  model: string;
  model_label: string;
  size: string;
  size_label: string;
  quality: string;
  count: number;
  api_size: string;
  status: string;
  progress: number;
  error: string;
  references: ImageTaskReference[];
  images: ImageTaskImage[];
}

export interface PublicImageTask {
  id: string;
  owner: string;
  created_at: number;
  updated_at: number;
  prompt: string;
  model: string;
  model_label: string;
  size: string;
  size_label: string;
  quality: string;
  count: number;
  api_size: string;
  status: string;
  progress: number;
  error: string;
  elapsed_ms: number;
  images: Array<{
    index: number;
    name: string;
    width: number;
    height: number;
    size_mb: number;
    created_at: number;
    url: string;
    download_url: string;
  }>;
  references: Array<{ index: number; name: string; url: string }>;
  active: boolean;
}

/** 接口调用可替换，测试里用桩；生产走真实客户端。 */
export interface ImagegenClients {
  generate: typeof generate_image;
  edit: typeof edit_image;
  gemini_generate: typeof gemini.generate_image;
  gemini_edit: typeof gemini.edit_image;
}

const DEFAULT_CLIENTS: ImagegenClients = {
  generate: generate_image,
  edit: edit_image,
  gemini_generate: gemini.generate_image,
  gemini_edit: gemini.edit_image,
};

/**
 * 与 Python 的 `raise ImageGenError(...)` 等价：message 是中文，status 给过滤器用。
 *
 * 必须抛 `HttpException`（而不是带 `status` 属性的普通 Error），否则全局异常过滤器
 * 认不出来，一律变成 500「服务器内部错误」。同时保留 `.status` 方便单测断言。
 */
export function imagegen_error(message: string, status = 400): HttpException {
  const text = String(message).slice(0, 600);
  const err = new HttpException({ ok: false, error: text }, status);
  (err as unknown as { status: number }).status = status;
  return err;
}

function now_seconds(): number {
  return Date.now() / 1000;
}

/** 按文件头猜扩展名（接口偶尔返回 JPEG/WEBP，别一律写 .png）。 */
export function sniff_extension(data: Buffer): string {
  if (data.length >= 8 && data.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return '.png';
  }
  if (data.length >= 3 && data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff) return '.jpg';
  if (
    data.length >= 12 &&
    data.subarray(0, 4).toString('latin1') === 'RIFF' &&
    data.subarray(8, 12).toString('latin1') === 'WEBP'
  ) {
    return '.webp';
  }
  if (data.length >= 3 && data.subarray(0, 3).toString('latin1') === 'GIF') return '.gif';
  return '.png';
}

function is_inside(dir: string, candidate: string): boolean {
  const relative = path.relative(dir, candidate);
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
}

@Injectable()
export class ImagegenManagerService {
  private readonly logger = new Logger(ImagegenManagerService.name);
  private readonly cfg: Record<string, unknown>;
  private readonly root: string;
  private readonly tasks: Map<string, ImageTaskRecord> = new Map();
  private readonly queue: string[] = [];
  private readonly waiters: Array<() => void> = [];
  private readonly clients: ImagegenClients;
  private workers_started = false;

  constructor(private readonly config: AppConfigService) {
    const raw =
      (config as unknown as { imagegenRaw?: Record<string, unknown> }).imagegenRaw ??
      config.imagegen ??
      readJsonFile<Record<string, unknown>>(path.join(config.serverDir, 'imagegen.json'), {});
    const source = raw !== null && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    this.cfg = { ...DEFAULT_IMAGE_CONFIG, ...(source as Record<string, unknown>) };
    this.clients = { ...DEFAULT_CLIENTS };

    this.root = path.join(config.dataDir, 'imagegen');
    fs.mkdirSync(this.tasks_dir, { recursive: true });
    fs.mkdirSync(this.files_dir, { recursive: true });
    this.load_existing();
  }

  /** 测试用：替换接口客户端；生产不调用。 */
  set_clients(patch: Partial<ImagegenClients>): void {
    Object.assign(this.clients, patch);
  }

  get tasks_dir(): string {
    return path.join(this.root, 'tasks');
  }

  get files_dir(): string {
    return path.join(this.root, 'files');
  }

  // ------------------------------------------------------------ 配置

  get enabled(): boolean {
    const raw = this.cfg.enabled;
    if (typeof raw === 'string') return !['0', 'false', 'no', 'off'].includes(raw.trim().toLowerCase());
    return Boolean(raw);
  }

  get base_url(): string {
    return String(this.cfg.base_url ?? '').trim();
  }

  get api_key(): string {
    return String(this.cfg.api_key ?? '').trim();
  }

  get gemini_base_url(): string {
    return String(this.cfg.gemini_base_url ?? '').trim();
  }

  get default_model(): string {
    return String(this.cfg.model ?? '').trim() || 'gpt-image-2-vip';
  }

  get default_quality(): string {
    return String(this.cfg.default_quality ?? '').trim() || 'high';
  }

  get timeout_seconds(): number {
    return clamp_timeout(this.cfg.timeout_seconds, 360);
  }

  get max_count(): number {
    return clamp_max_count(this.cfg.max_count, 4);
  }

  get max_references(): number {
    return clamp_max_references(this.cfg.max_references, 10);
  }

  get max_inflight(): number {
    return clamp_max_inflight(this.cfg.max_inflight, 3);
  }

  get price_per_image(): string {
    return String(this.cfg.price_per_image ?? '0.03');
  }

  get configured(): boolean {
    return this.enabled && Boolean(this.api_key) && Boolean(this.base_url);
  }

  unconfigured_reason(): string {
    if (!this.enabled) return '生图功能已关闭（server/imagegen.json → enabled=false）。';
    if (!this.base_url) return '生图接口地址未配置：server/imagegen.json → base_url。';
    if (!this.api_key) return '生图密钥未配置：在 server/imagegen.json 里填 api_key（老张 API Key）。';
    return '';
  }

  /** 前端 bootstrap 用的下拉数据；字段名与 Python `options()` 完全一致。 */
  options(): Record<string, unknown> {
    const default_model = this.default_model;
    const quality_pairs = _quality_pairs(default_model);
    const default_quality = quality_pairs.some(([value]) => value === this.default_quality)
      ? this.default_quality
      : 'auto';
    return {
      configured: this.configured,
      reason: this.unconfigured_reason(),
      models: IMAGE_MODEL_IDS.map((model_id) => ({
        id: model_id,
        label: image_model_label(model_id),
        provider: this.model_provider(model_id),
        price: String(this.price_of(model_id)),
        sizes: _size_options(model_id),
        qualities: _quality_pairs(model_id).map(([value, label]) => ({ value, label })),
      })),
      default_model,
      default_quality,
      max_count: this.max_count,
      max_references: this.max_references,
      price: this.price_per_image,
      prompt_limit: IMAGE_PROMPT_LIMIT,
    };
  }

  private model_provider(model: string): string {
    return image_model_provider(model);
  }

  private price_of(model: string): string {
    return image_model_spec(model)?.price_usd ?? this.price_per_image;
  }

  // ------------------------------------------------------------ 查询

  list_tasks(user: ImagegenUser | undefined, options: { limit?: number } = {}): PublicImageTask[] {
    const username = String(user?.username ?? '');
    const is_admin = String(user?.role ?? '') === 'admin';
    const limit = Math.max(1, Math.min(200, _as_int(options.limit, 40)));
    const items = Array.from(this.tasks.values()).filter((record) => is_admin || record.owner === username);
    items.sort((a, b) => b.created_at - a.created_at);
    return items.slice(0, limit).map((record) => this.public(record));
  }

  get_record(task_id: string, user: ImagegenUser | undefined): PublicImageTask {
    return this.public(this.authorized(task_id, user));
  }

  private authorized(task_id: string, user: ImagegenUser | undefined): ImageTaskRecord {
    const record = this.tasks.get(String(task_id ?? '').trim());
    const is_admin = String(user?.role ?? '') === 'admin';
    if (record === undefined || (!is_admin && record.owner !== String(user?.username ?? ''))) {
      // 不区分「不存在」和「不是你的」，避免泄露 id 有效性。
      throw imagegen_error('找不到这个生图任务。', 404);
    }
    return record;
  }

  public(record: ImageTaskRecord): PublicImageTask {
    const id = record.id;
    return {
      id,
      owner: record.owner,
      created_at: record.created_at,
      updated_at: record.updated_at,
      prompt: record.prompt,
      model: record.model,
      model_label: record.model_label,
      size: record.size,
      size_label: record.size_label,
      quality: record.quality,
      count: record.count,
      api_size: record.api_size,
      status: record.status,
      progress: record.progress,
      error: record.error,
      elapsed_ms: record.elapsed_ms,
      images: record.images.map((image) => ({
        index: image.index,
        name: image.name,
        width: image.width,
        height: image.height,
        size_mb: image.size_mb,
        created_at: image.created_at,
        url: `/api/imagegen/tasks/${id}/images/${image.index}`,
        download_url: `/api/imagegen/tasks/${id}/images/${image.index}?download=1`,
      })),
      references: record.references.map((reference) => ({
        index: reference.index,
        name: reference.name,
        url: `/api/imagegen/tasks/${id}/references/${reference.index}`,
      })),
      active: ACTIVE_STATUSES.has(record.status),
    };
  }

  // ------------------------------------------------------------ 建任务

  create_task(
    user: ImagegenUser | undefined,
    payload: Record<string, unknown> = {},
    references: ImageReferenceSlot[] = [],
  ): PublicImageTask {
    if (!this.configured) throw imagegen_error(this.unconfigured_reason() || '生图功能未配置。', 503);

    const username = String(user?.username ?? '').trim();
    if (!username) throw imagegen_error('请先登录。', 401);

    const prompt = String(payload.prompt ?? '').trim();
    if (!prompt) throw imagegen_error('请先填写提示词。');
    // Python 的 len() 数字符（码点），JS 的 .length 数 UTF-16 单元，emoji 会差一倍。
    const prompt_length = Array.from(prompt).length;
    if (prompt_length > IMAGE_PROMPT_LIMIT) {
      throw imagegen_error(`提示词过长（${prompt_length} 字），上限 ${IMAGE_PROMPT_LIMIT} 字。`);
    }

    const requested_model = String(payload.model ?? '').trim();
    const model = requested_model || this.default_model;
    if (!IMAGE_MODEL_IDS.includes(model)) throw imagegen_error('不支持的图片模型。');

    const size_values = _size_options(model).map((option) => option.value);
    const requested_size = String(payload.size ?? '').trim();
    const size = requested_size || 'auto';
    if (!size_values.includes(size)) throw imagegen_error('所选模型不支持这个输出尺寸。');

    const quality_values = image_quality_options(model);
    const requested_quality = String(payload.quality ?? '').trim();
    let quality: string;
    if (requested_quality) {
      if (!quality_values.includes(requested_quality)) throw imagegen_error('所选模型不支持此图片质量');
      quality = requested_quality;
    } else {
      quality = quality_values.includes(this.default_quality) ? this.default_quality : quality_values[0];
    }

    try {
      // 各协议再自检一次（Gemini 的 size 必须在比例表里，Images 的 quality 必须在档位里）。
      image_request_parameters(model, size, quality);
    } catch (err) {
      throw imagegen_error(explain_error(err));
    }

    const count = Math.max(1, Math.min(this.max_count, _as_int(payload.count, 1)));
    if (references.length > this.max_references) {
      throw imagegen_error(`参考图最多 ${this.max_references} 张。`);
    }

    const task_id = randomUUID().replace(/-/g, '');
    const task_dir = path.join(this.files_dir, task_id);
    const created = now_seconds();
    const record: ImageTaskRecord = {
      id: task_id,
      owner: username,
      created_at: created,
      updated_at: created,
      started_at: 0,
      finished_at: 0,
      elapsed_ms: 0,
      prompt,
      model,
      model_label: image_model_label(model),
      size,
      size_label: this.size_label(model, size),
      quality,
      count,
      api_size: this.api_size(model, size),
      status: STATUS_QUEUED,
      progress: 0,
      error: '',
      references: [],
      images: [],
    };

    try {
      fs.mkdirSync(task_dir, { recursive: true });
      references.forEach((reference, offset) => {
        const index = offset + 1;
        const source = path.resolve(String(reference.path ?? ''));
        if (!fs.existsSync(source) || !fs.statSync(source).isFile()) {
          throw imagegen_error('参考图已失效，请重新上传。', 404);
        }
        const suffix = path.extname(source).toLowerCase() || '.png';
        const target = path.join(task_dir, `ref${index}${suffix}`);
        fs.copyFileSync(source, target);
        record.references.push({
          index,
          name: String(reference.name ?? '') || path.basename(source),
          upload_id: String(reference.upload_id ?? ''),
          file: path.basename(target),
        });
      });
    } catch (err) {
      fs.rmSync(task_dir, { recursive: true, force: true });
      throw err;
    }

    this.tasks.set(task_id, record);
    this.persist(record);
    this.enqueue(task_id);
    this.ensure_workers();
    return this.public(record);
  }

  private size_label(model: string, size: string): string {
    if (is_gemini_model(model)) return GEMINI_SIZE_BY_VALUE.get(size)?.label ?? size;
    return output_spec_label(size);
  }

  private api_size(model: string, size: string): string {
    if (is_gemini_model(model)) {
      const spec = GEMINI_SIZE_BY_VALUE.get(size);
      if (spec === undefined || !spec.image_size) return 'auto';
      return `${spec.image_size}（${spec.aspect_ratio}）`;
    }
    if (!supports_image_parameters(model)) return 'auto';
    return api_size_for_output(size, model);
  }

  // ------------------------------------------------------------ 删除 / 取文件

  delete_task(task_id: string, user: ImagegenUser | undefined): void {
    const record = this.authorized(task_id, user);
    if (ACTIVE_STATUSES.has(record.status)) throw imagegen_error('任务还在生成中，请等它跑完再删。', 409);

    this.tasks.delete(record.id);
    fs.rmSync(path.join(this.tasks_dir, `${record.id}.json`), { force: true });
    fs.rmSync(path.join(this.files_dir, record.id), { recursive: true, force: true });
  }

  image_file(
    task_id: string,
    index: number,
    user: ImagegenUser | undefined,
    options: { reference?: boolean } = {},
  ): { file: string; name: string } {
    const record = this.authorized(task_id, user);
    const task_dir = path.join(this.files_dir, record.id);

    if (options.reference) {
      const reference = record.references.find((item) => item.index === index);
      if (reference === undefined) throw imagegen_error('找不到这张参考图。', 404);
      const file = path.resolve(task_dir, reference.file);
      if (!is_inside(task_dir, file) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        throw imagegen_error('找不到这张参考图。', 404);
      }
      return { file, name: reference.name };
    }

    const image = record.images.find((item) => item.index === index);
    if (image === undefined) throw imagegen_error('找不到这张图片。', 404);
    const file = path.resolve(task_dir, image.file);
    if (!is_inside(task_dir, file) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      throw imagegen_error('图片文件已丢失。', 404);
    }
    return { file, name: image.name };
  }

  // ------------------------------------------------------------ 落盘

  private persist(record: ImageTaskRecord): void {
    record.updated_at = now_seconds();
    const file = path.join(this.tasks_dir, `${record.id}.json`);
    const temp = `${file}.tmp`;
    try {
      fs.writeFileSync(temp, JSON.stringify(record, null, 1), 'utf8');
      fs.renameSync(temp, file);
    } catch (err) {
      // 落盘失败不影响内存里的任务（与 Python 一致：只记日志，不抛）。
      this.logger.warn(`生图任务落盘失败：${record.id} ${explain_error(err)}`);
    }
  }

  /** 启动时把上次进程留下的「排队中/生成中」落成失败，避免页面上永远转圈。 */
  private load_existing(): void {
    let names: string[] = [];
    try {
      names = fs.readdirSync(this.tasks_dir).filter((name) => name.endsWith('.json')).sort();
    } catch {
      return;
    }
    for (const name of names) {
      let record: ImageTaskRecord;
      try {
        const parsed = JSON.parse(fs.readFileSync(path.join(this.tasks_dir, name), 'utf8')) as ImageTaskRecord;
        if (parsed === null || typeof parsed !== 'object' || !parsed.id) continue;
        record = parsed;
      } catch {
        continue;
      }
      if (ACTIVE_STATUSES.has(record.status)) {
        record.status = record.images.length > 0 ? STATUS_PARTIAL : STATUS_FAILED;
        record.error = record.error || '服务重启，任务中断，请重新提交。';
        record.finished_at = record.finished_at || now_seconds();
        this.persist(record);
      }
      this.tasks.set(record.id, record);
    }
  }

  // ------------------------------------------------------------ 队列 / worker

  private enqueue(task_id: string): void {
    this.queue.push(task_id);
    const waiter = this.waiters.shift();
    if (waiter) waiter();
  }

  private async next_task(): Promise<string> {
    while (this.queue.length === 0) {
      await new Promise<void>((resolve) => this.waiters.push(resolve));
    }
    return this.queue.shift() as string;
  }

  private ensure_workers(): void {
    if (this.workers_started) return;
    this.workers_started = true;
    for (let index = 0; index < this.max_inflight; index += 1) {
      void this.loop();
    }
  }

  private async loop(): Promise<void> {
    for (;;) {
      const task_id = await this.next_task();
      const record = this.tasks.get(task_id);
      if (record === undefined) continue;
      try {
        await this.run(record);
      } catch (err) {
        const current = this.tasks.get(task_id);
        if (current !== undefined && ACTIVE_STATUSES.has(current.status)) {
          current.status = STATUS_FAILED;
          current.error = '生图线程发生未预期异常，请重试。';
          this.logger.warn(`生图任务异常：${task_id} ${explain_error(err)}`);
          this.persist(current);
        }
      }
    }
  }

  private async run(record: ImageTaskRecord): Promise<void> {
    const started = now_seconds();
    record.status = STATUS_RUNNING;
    record.started_at = started;
    record.error = '';
    record.progress = 0;
    this.persist(record);

    const task_dir = path.join(this.files_dir, record.id);
    const reference_paths = record.references.map((reference) => path.join(task_dir, reference.file));
    const total = Math.max(1, record.count);

    for (let index = 1; index <= total; index += 1) {
      record.progress = index - 1;
      this.persist(record);

      let raw: Buffer;
      try {
        raw = await this.call_provider(record, reference_paths);
      } catch (err) {
        this.fail(record, index, err);
        return;
      }

      let finalized: { data: Buffer; width: number; height: number };
      try {
        finalized = await finalize_output_image(raw, record.size);
      } catch (err) {
        this.fail(record, index, err);
        return;
      }

      const extension = sniff_extension(finalized.data);
      const filename = `${index}${extension}`;
      try {
        fs.writeFileSync(path.join(task_dir, filename), finalized.data);
        const sidecar = {
          task_id: record.id,
          index,
          prompt: record.prompt,
          model: record.model,
          size: record.size,
          api_size: record.api_size,
          quality: record.quality,
          output_size: [finalized.width, finalized.height],
          reference_images: record.references.map((reference) => reference.file),
          created_at: now_seconds(),
          operator: record.owner,
        };
        fs.writeFileSync(path.join(task_dir, `${index}.meta.json`), JSON.stringify(sidecar, null, 1), 'utf8');
      } catch (err) {
        this.fail(record, index, err);
        return;
      }

      record.images.push({
        index,
        file: filename,
        name: `${record.id.slice(0, 8)}-${index}${extension}`,
        width: finalized.width,
        height: finalized.height,
        size_mb: Math.round((finalized.data.length / 1048576) * 100) / 100,
        created_at: now_seconds(),
      });
      record.progress = index;
      this.persist(record);
    }

    record.status = STATUS_DONE;
    record.finished_at = now_seconds();
    record.elapsed_ms = Math.trunc((record.finished_at - started) * 1000);
    this.persist(record);
  }

  private async call_provider(record: ImageTaskRecord, reference_paths: string[]): Promise<Buffer> {
    if (is_gemini_model(record.model)) {
      // Gemini 的 size 原样传「比例@档位」，返回什么比例就是成品比例，不再本地裁切。
      const options = {
        base_url: this.gemini_base_url,
        model: record.model,
        quality: record.quality || 'auto',
        timeout: this.timeout_seconds,
      };
      if (reference_paths.length > 0) {
        return this.clients.gemini_edit(record.prompt, record.size, reference_paths, this.api_key, options);
      }
      return this.clients.gemini_generate(record.prompt, record.size, this.api_key, options);
    }

    const options = {
      base_url: this.base_url,
      model: record.model,
      quality: record.quality,
      timeout: this.timeout_seconds,
    };
    if (reference_paths.length > 0) {
      return this.clients.edit(record.prompt, record.api_size, reference_paths, this.api_key, options);
    }
    return this.clients.generate(record.prompt, record.api_size, this.api_key, options);
  }

  private fail(record: ImageTaskRecord, index: number, error: unknown): void {
    const message = explain_error(error);
    record.error = record.images.length > 0 ? `第 ${index} 张：${message}` : message;
    record.status = record.images.length > 0 ? STATUS_PARTIAL : STATUS_FAILED;
    record.finished_at = now_seconds();
    if (record.started_at) {
      record.elapsed_ms = Math.trunc((record.finished_at - record.started_at) * 1000);
    }
    this.logger.warn(`生图任务失败：${record.id} ${record.error}`);
    this.persist(record);
  }
}
