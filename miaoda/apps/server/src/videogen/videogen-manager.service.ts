/**
 * 自由视频任务管理器（对应 `app/videogen/manager.py`）。
 *
 * 设计取舍与换装链路刻意分开：
 *  * 不读模板清单、不打标签、不归档 COS；成品直接落 `<data>/videogen/files/<task>/shot-<n>.mp4`。
 *  * 独立队列 + 独立并发（`web.json.videogen_max_inflight`，默认 2），不占 `max_active_jobs`。
 *  * 单任务多分镜顺序跑，失败不影响后面的分镜；云端在飞走「快速通道」信号量。
 *  * 记录里只存文件名，绝对路径读取时按当前 data_dir 重新拼（换机器不悬空）。
 */
import { HttpException, Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { randomBytes } from 'node:crypto';
import * as fs from 'node:fs';
import * as fsp from 'node:fs/promises';
import * as path from 'node:path';

import { AppConfigService } from '../config/app-config.service';
import { StorageService } from '../storage/storage.service';
import { readJsonFile, writeJsonAtomic } from '../common/fs-json';
import {
  TaskCanceled,
  TaskExpired,
  WaitStopped,
  WaitTimeout,
  WanClientService,
  WanError,
} from '../wan/wan-client.service';
import {
  MAX_IMAGES,
  MAX_VIDEO_TOTAL,
  VideoGenError,
  buildOptions,
  nowText,
  validateRequest,
  type VideoGenOptions,
} from './videogen-models';
import {
  base64BudgetError,
  toDataUri,
  validateAudio,
  validateVideo,
} from './videogen-media';

export const STATUS_QUEUED = '排队中';
export const STATUS_RUNNING = '生成中';
export const STATUS_DONE = '已完成';
export const STATUS_PARTIAL = '部分完成';
export const STATUS_FAILED = '生成失败';
export const STATUS_CANCELED = '已取消';
export const ACTIVE_STATUSES = [STATUS_QUEUED, STATUS_RUNNING];

export const SHOT_QUEUED = '排队中';
export const SHOT_RUNNING = '生成中';
export const SHOT_DONE = '已完成';
export const SHOT_FAILED = '生成失败';
export const SHOT_CANCELED = '已取消';

export const UPLOAD_KINDS: Record<string, { suffixes: string[]; maxMb: number }> = {
  video: { suffixes: ['.mp4', '.mov', '.m4v'], maxMb: 100 },
  audio: { suffixes: ['.wav', '.mp3'], maxMb: 15 },
};

const TASK_ID_RE = /^[a-f0-9]{32}$/;

export interface VgShot {
  index: number;
  status: string;
  file: string;
  error: string;
  message: string;
  task_id: string;
  seconds: number;
  billable_seconds: number;
  estimated_cost: number | null;
}

export interface VgRef {
  index: number;
  file: string;
  name: string;
  size_mb: number | null;
  duration?: number | null;
  width?: number | null;
  height?: number | null;
}

export interface VgRecord {
  id: string;
  owner: string;
  created_at: string;
  updated_at: string;
  started_at: string;
  finished_at: string;
  status: string;
  progress: string;
  prompt: string;
  negative_prompt: string;
  mode_choice: string;
  mode: string;
  resolution: string;
  ratio: string;
  duration: number;
  count: number;
  seed: number | null;
  audio: boolean;
  prompt_extend: boolean;
  watermark: boolean;
  video_seconds: number | null;
  refs: { images: VgRef[]; videos: VgRef[]; audios: VgRef[] };
  shots: VgShot[];
  message: string;
  cancel_requested: boolean;
}

export interface VgUpload {
  upload_id: string;
  owner: string;
  kind: string;
  name: string;
  file: string;
  size_mb: number;
  created_at: string;
  duration?: number | null;
  width?: number | null;
  height?: number | null;
}

export interface Actor {
  username: string;
  role: string;
}

@Injectable()
export class VideogenManagerService implements OnModuleInit {
  private readonly logger = new Logger(VideogenManagerService.name);
  private readonly records = new Map<string, VgRecord>();
  private readonly queue: string[] = [];
  private running = 0;

  readonly root: string;
  readonly tasksDir: string;
  readonly filesDir: string;
  readonly uploadsDir: string;
  readonly tmpDir: string;

  constructor(
    private readonly config: AppConfigService,
    private readonly wan: WanClientService,
    private readonly storage: StorageService,
  ) {
    this.root = path.join(config.dataDir, 'videogen');
    this.tasksDir = path.join(this.root, 'tasks');
    this.filesDir = path.join(this.root, 'files');
    this.uploadsDir = path.join(this.root, 'uploads');
    this.tmpDir = path.join(this.root, 'tmp');
  }

  onModuleInit(): void {
    for (const dir of [this.tasksDir, this.filesDir, this.uploadsDir, this.tmpDir]) {
      fs.mkdirSync(dir, { recursive: true });
    }
    this.loadExisting();
  }

  // ---------------------------------------------------------------- 配置
  get configured(): boolean {
    return Boolean(this.config.apiKey && this.config.model);
  }

  unconfiguredReason(): string {
    if (this.configured) return '';
    return '未配置 wan.api_key / wan.model，自由视频不可用。';
  }

  options(): VideoGenOptions {
    return buildOptions({
      configured: this.configured,
      reason: this.unconfiguredReason(),
      model: this.config.model || 'wan3.0-video',
      provider: this.config.provider || 'dashscope',
      cosEnabled: this.config.cosEnabled,
    });
  }

  // ------------------------------------------------------------ 持久化
  private recordPath(taskId: string): string {
    return path.join(this.tasksDir, `${taskId}.json`);
  }

  private taskDir(taskId: string): string {
    return path.join(this.filesDir, taskId);
  }

  private persist(record: VgRecord): void {
    writeJsonAtomic(this.recordPath(record.id), record);
  }

  /** 重启恢复：进行中的任务直接落成失败（云端可能已出片并计费）。 */
  private loadExisting(): void {
    let names: string[] = [];
    try {
      names = fs.readdirSync(this.tasksDir).filter((name) => name.endsWith('.json')).sort();
    } catch {
      return;
    }
    for (const name of names) {
      const record = readJsonFile<VgRecord | null>(path.join(this.tasksDir, name), null);
      if (!record || !record.id) continue;
      if (ACTIVE_STATUSES.includes(record.status)) {
        record.status = STATUS_FAILED;
        record.message =
          '服务重启，进行中的任务已中止；云端可能已出片并计费，请到百炼控制台确认。';
        for (const shot of record.shots || []) {
          if (shot.status === SHOT_QUEUED || shot.status === SHOT_RUNNING) {
            shot.status = SHOT_FAILED;
            shot.error = '服务重启中止';
          }
        }
        record.finished_at = record.finished_at || nowText();
        try {
          this.persist(record);
        } catch (err) {
          this.logger.warn(`恢复任务 ${record.id} 写盘失败：${(err as Error).message}`);
        }
      }
      this.records.set(record.id, record);
    }
  }

  // -------------------------------------------------------------- 上传
  addUpload(actor: Actor, tempFile: string, filename: string, kindRaw: string): VgUpload {
    const kind = String(kindRaw || '').trim().toLowerCase();
    const spec = UPLOAD_KINDS[kind];
    if (!spec) throw new VideoGenError('素材类型只能是 video 或 audio。');
    const suffix = path.extname(filename).toLowerCase();
    if (!spec.suffixes.includes(suffix)) {
      const allowed = [...spec.suffixes].sort().join(' / ');
      throw new VideoGenError(`参考${kind === 'video' ? '视频' : '音频'}只支持 ${allowed}。`);
    }
    const sizeMb = Math.round((fs.statSync(tempFile).size / 1024 / 1024) * 1000) / 1000;
    if (sizeMb > spec.maxMb) {
      throw new VideoGenError(
        `参考素材 ${sizeMb.toFixed(1)}MB，超过 ${spec.maxMb}MB 上限。`,
        413,
      );
    }
    const info = kind === 'video' ? validateVideo(tempFile) : validateAudio(tempFile);

    const uploadId = randomId();
    const dest = path.join(this.uploadsDir, `${uploadId}${suffix}`);
    fs.renameSync(tempFile, dest);
    const meta: VgUpload = {
      upload_id: uploadId,
      owner: actor.username,
      kind,
      name: path.basename(filename) || `${kind}${suffix}`,
      file: path.basename(dest),
      size_mb: sizeMb,
      created_at: nowText(),
      duration: (info as { duration?: number }).duration ?? null,
      width: (info as { width?: number }).width ?? null,
      height: (info as { height?: number }).height ?? null,
    };
    writeJsonAtomic(path.join(this.uploadsDir, `${uploadId}.json`), meta);
    return {
      upload_id: meta.upload_id,
      kind: meta.kind,
      name: meta.name,
      size_mb: meta.size_mb,
      duration: meta.duration ?? null,
      width: meta.width ?? null,
      height: meta.height ?? null,
    } as VgUpload;
  }

  private resolveUpload(uploadId: string, actor: Actor, kind: string): VgUpload & { path: string } {
    const id = String(uploadId || '').trim();
    if (!TASK_ID_RE.test(id)) throw new VideoGenError('参考素材已失效，请重新上传。', 404);
    const meta = readJsonFile<VgUpload | null>(path.join(this.uploadsDir, `${id}.json`), null);
    if (!meta) throw new VideoGenError('参考素材已失效，请重新上传。', 404);
    if (meta.kind !== kind) throw new VideoGenError('参考素材类型不匹配。');
    if (meta.owner !== actor.username && actor.role !== 'admin') {
      throw new VideoGenError('找不到这个参考素材。', 404);
    }
    const file = path.join(this.uploadsDir, String(meta.file || ''));
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) {
      throw new VideoGenError('参考素材文件已丢失，请重新上传。', 404);
    }
    return { ...meta, path: file };
  }

  // ------------------------------------------------------------ 创建任务
  createTask(
    actor: Actor,
    payload: Record<string, unknown>,
    images: Array<{ path: string; name: string }>,
  ): Record<string, unknown> {
    if (!this.configured) throw new VideoGenError(this.unconfiguredReason(), 503);

    const videoIds = idList(payload.video_ids, '参考视频');
    const audioIds = idList(payload.audio_ids, '参考音频');
    const videos = videoIds.map((id) => this.resolveUpload(id, actor, 'video'));
    const audios = audioIds.map((id) => this.resolveUpload(id, actor, 'audio'));
    const slots = [...(images || [])];
    if (slots.length > MAX_IMAGES) throw new VideoGenError(`参考图最多 ${MAX_IMAGES} 张。`);

    const fields = validateRequest(payload, {
      imageCount: slots.length,
      videoMetas: videos,
      audioCount: audios.length,
    });

    const taskId = randomId();
    const dir = this.taskDir(taskId);
    fs.mkdirSync(dir, { recursive: true });

    const refs: VgRecord['refs'] = { images: [], videos: [], audios: [] };
    try {
      slots.forEach((slot, offset) => {
        const index = offset + 1;
        const src = String(slot.path || '');
        if (!src || !fs.existsSync(src) || !fs.statSync(src).isFile()) {
          throw new VideoGenError(`第 ${index} 张参考图已失效，请重新上传。`, 404);
        }
        const suffix = path.extname(src).toLowerCase();
        const destName = `image${index}${suffix}`;
        fs.copyFileSync(src, path.join(dir, destName));
        refs.images.push({
          index,
          file: destName,
          name: String(slot.name || path.basename(src)),
          size_mb: Math.round((fs.statSync(src).size / 1024 / 1024) * 1000) / 1000,
          width: null,
          height: null,
        });
      });
      videos.forEach((meta, offset) => {
        const index = offset + 1;
        const suffix = path.extname(meta.path).toLowerCase();
        const destName = `video${index}${suffix}`;
        fs.copyFileSync(meta.path, path.join(dir, destName));
        refs.videos.push({
          index,
          file: destName,
          name: meta.name || path.basename(meta.path),
          size_mb: meta.size_mb,
          duration: meta.duration ?? null,
          width: meta.width ?? null,
          height: meta.height ?? null,
        });
      });
      audios.forEach((meta, offset) => {
        const index = offset + 1;
        const suffix = path.extname(meta.path).toLowerCase();
        const destName = `audio${index}${suffix}`;
        fs.copyFileSync(meta.path, path.join(dir, destName));
        refs.audios.push({
          index,
          file: destName,
          name: meta.name || path.basename(meta.path),
          size_mb: meta.size_mb,
        });
      });
    } catch (err) {
      fs.rmSync(dir, { recursive: true, force: true });
      throw err;
    }

    const record: VgRecord = {
      id: taskId,
      owner: actor.username,
      created_at: nowText(),
      updated_at: nowText(),
      started_at: '',
      finished_at: '',
      status: STATUS_QUEUED,
      progress: `0/${fields.count}`,
      prompt: fields.prompt,
      negative_prompt: fields.negative_prompt,
      mode_choice: fields.mode_choice,
      mode: fields.mode,
      resolution: fields.resolution,
      ratio: fields.ratio,
      duration: fields.duration,
      count: fields.count,
      seed: fields.seed,
      audio: fields.audio,
      prompt_extend: fields.prompt_extend,
      watermark: fields.watermark,
      video_seconds: fields.video_seconds,
      refs,
      shots: Array.from({ length: fields.count }, (_, offset) => ({
        index: offset + 1,
        status: SHOT_QUEUED,
        file: '',
        error: '',
        message: '',
        task_id: '',
        seconds: 0,
        billable_seconds: 0,
        estimated_cost: null,
      })),
      message: '',
      cancel_requested: false,
    };
    this.records.set(taskId, record);
    this.persist(record);
    this.queue.push(taskId);
    this.pump();
    return this.publicView(record, actor.role === 'admin', true);
  }

  // ------------------------------------------------------------------ 查询
  private authorized(taskId: string, actor: Actor): VgRecord {
    const id = String(taskId || '');
    if (!TASK_ID_RE.test(id)) throw new VideoGenError('找不到这个视频任务。', 404);
    let record = this.records.get(id);
    if (!record) {
      record = readJsonFile<VgRecord | null>(this.recordPath(id), null) || undefined;
      if (!record) throw new VideoGenError('找不到这个视频任务。', 404);
      this.records.set(id, record);
    }
    if (record.owner !== actor.username && actor.role !== 'admin') {
      throw new VideoGenError('找不到这个视频任务。', 404);
    }
    return record;
  }

  getRecord(taskId: string, actor: Actor): Record<string, unknown> {
    return this.publicView(this.authorized(taskId, actor), actor.role === 'admin', true);
  }

  listTasks(actor: Actor, limit = 40): Array<Record<string, unknown>> {
    const admin = actor.role === 'admin';
    let records = [...this.records.values()];
    if (!admin) records = records.filter((item) => item.owner === actor.username);
    records.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    const capped = Math.max(1, Math.min(200, Math.trunc(limit || 40)));
    return records.slice(0, capped).map((item) => this.publicView(item, admin, true));
  }

  videoFile(taskId: string, index: number, actor: Actor): { path: string; name: string } {
    const record = this.authorized(taskId, actor);
    const shot = (record.shots || []).find((item) => Number(item.index) === Number(index));
    if (!shot || !shot.file) throw new VideoGenError('这条视频还没有生成。', 404);
    const file = path.join(this.taskDir(record.id), String(shot.file));
    if (!fs.existsSync(file)) throw new VideoGenError('视频文件已丢失。', 404);
    return { path: file, name: `${record.id}-${Math.trunc(index)}.mp4` };
  }

  referenceFile(
    taskId: string,
    kind: string,
    index: number,
    actor: Actor,
  ): { path: string; name: string } {
    const record = this.authorized(taskId, actor);
    if (!['images', 'videos', 'audios'].includes(kind)) {
      throw new VideoGenError('找不到这个参考素材。', 404);
    }
    const list = record.refs?.[kind as keyof VgRecord['refs']] || [];
    const ref = (list as VgRef[]).find((item) => Number(item.index) === Number(index));
    if (!ref || !ref.file) throw new VideoGenError('找不到这个参考素材。', 404);
    const file = path.join(this.taskDir(record.id), String(ref.file));
    if (!fs.existsSync(file)) throw new VideoGenError('参考素材已丢失。', 404);
    return { path: file, name: String(ref.name || path.basename(file)) };
  }

  // ----------------------------------------------------------- 取消/删除
  cancelTask(taskId: string, actor: Actor): Record<string, unknown> {
    const record = this.authorized(taskId, actor);
    if (!ACTIVE_STATUSES.includes(record.status)) {
      throw new VideoGenError('这个任务已经结束，不能取消。', 409);
    }
    record.cancel_requested = true;
    if (record.status === STATUS_QUEUED) {
      record.status = STATUS_CANCELED;
      record.finished_at = nowText();
      for (const shot of record.shots || []) {
        if (shot.status === SHOT_QUEUED) shot.status = SHOT_CANCELED;
      }
    } else {
      record.message = '正在取消：当前这一条会尽快停下（云端任务不会被撤回）。';
    }
    record.updated_at = nowText();
    this.persist(record);
    return this.publicView(record, actor.role === 'admin', true);
  }

  deleteTask(taskId: string, actor: Actor): void {
    const record = this.authorized(taskId, actor);
    if (ACTIVE_STATUSES.includes(record.status)) {
      throw new VideoGenError('任务还在跑，先取消再删除。', 409);
    }
    this.records.delete(record.id);
    try {
      fs.unlinkSync(this.recordPath(record.id));
    } catch {
      /* 文件已经不在了 */
    }
    fs.rmSync(this.taskDir(record.id), { recursive: true, force: true });
  }

  // ------------------------------------------------------------------ 输出
  publicView(record: VgRecord, admin = false, detail = true): Record<string, unknown> {
    const taskId = record.id;
    const shots = (record.shots || []).map((shot) => {
      const done = Boolean(shot.file);
      const item: Record<string, unknown> = {
        index: Number(shot.index) || 0,
        status: shot.status || '',
        error: shot.error || '',
        message: shot.message || '',
        task_id: shot.task_id || '',
        seconds: shot.seconds || 0,
        url: done ? `/api/videogen/tasks/${taskId}/videos/${shot.index}` : '',
        download_url: done ? `/api/videogen/tasks/${taskId}/videos/${shot.index}?download=1` : '',
      };
      if (admin) {
        item.billable_seconds = shot.billable_seconds || 0;
        item.estimated_cost = shot.estimated_cost ?? null;
      }
      return item;
    });

    const out: Record<string, unknown> = {
      id: taskId,
      owner: record.owner || '',
      created_at: record.created_at || '',
      updated_at: record.updated_at || '',
      started_at: record.started_at || '',
      finished_at: record.finished_at || '',
      status: record.status || '',
      progress: record.progress || '',
      prompt: record.prompt || '',
      negative_prompt: record.negative_prompt || '',
      mode: record.mode || '',
      mode_choice: record.mode_choice || '',
      resolution: record.resolution || '',
      ratio: record.ratio || '',
      duration: record.duration,
      count: record.count || 1,
      seed: record.seed,
      audio: Boolean(record.audio),
      prompt_extend: Boolean(record.prompt_extend),
      watermark: Boolean(record.watermark),
      video_seconds: record.video_seconds,
      message: record.message || '',
      active: ACTIVE_STATUSES.includes(record.status),
      cancel_requested: Boolean(record.cancel_requested),
    };
    if (detail) {
      out.shots = shots;
      out.refs = this.publicRefs(record);
    }
    return out;
  }

  private publicRefs(record: VgRecord): Record<string, unknown[]> {
    const taskId = record.id;
    const out: Record<string, unknown[]> = {};
    for (const kind of ['images', 'videos', 'audios'] as const) {
      out[kind] = (record.refs?.[kind] || []).map((ref) => ({
        index: Number(ref.index) || 0,
        name: ref.name || '',
        size_mb: ref.size_mb ?? null,
        duration: ref.duration ?? null,
        width: ref.width ?? null,
        height: ref.height ?? null,
        url: `/api/videogen/tasks/${taskId}/refs/${kind}/${ref.index}`,
      }));
    }
    return out;
  }

  // ------------------------------------------------------------------ 队列
  private pump(): void {
    const limit = this.config.videogenMaxInflight;
    while (this.running < limit && this.queue.length > 0) {
      const taskId = this.queue.shift() as string;
      this.running += 1;
      void this.runTask(taskId)
        .catch((err) => this.logger.error(`videogen 任务 ${taskId} 异常：${(err as Error).message}`))
        .finally(() => {
          this.running -= 1;
          this.pump();
        });
    }
  }

  /** 参考视频/音频先传 COS，再给 7 天有效的预签名地址（官方只收 URL，不收 Base64）。 */
  private async uploadRefs(record: VgRecord, files: string[]): Promise<string[]> {
    const urls: string[] = [];
    for (const file of files) {
      const key = `videogen/ref/${record.id}/${path.basename(file)}`;
      await this.storage.putFile(file, key, this.storage.contentType(file));
      urls.push(this.storage.signUrl(key, 7 * 24 * 3600));
    }
    return urls;
  }

  private async runTask(taskId: string): Promise<void> {
    const record = this.records.get(taskId);
    if (!record) return;
    if (record.cancel_requested || record.status !== STATUS_QUEUED) {
      for (const shot of record.shots || []) {
        if (shot.status === SHOT_QUEUED) {
          shot.status = SHOT_CANCELED;
          shot.error = '已取消';
        }
      }
      record.status = STATUS_CANCELED;
      this.finish(record);
      return;
    }

    record.status = STATUS_RUNNING;
    record.started_at = nowText();
    record.updated_at = record.started_at;
    this.persist(record);

    try {
      const dir = this.taskDir(record.id);
      const imageFiles = record.refs.images.map((ref) => path.join(dir, ref.file));
      const videoFiles = record.refs.videos.map((ref) => path.join(dir, ref.file));
      const audioFiles = record.refs.audios.map((ref) => path.join(dir, ref.file));

      if ((videoFiles.length > 0 || audioFiles.length > 0) && !this.config.cosEnabled) {
        this.failAll(
          record,
          '参考视频/音频需要开启 COS 才能上传（官方不支持 Base64）：未启用 COS',
        );
        return;
      }
      const budget = base64BudgetError(imageFiles, this.config.cosEnabled);
      if (budget) {
        this.failAll(record, budget);
        return;
      }

      let imageUrls: string[] = [];
      try {
        imageUrls = imageFiles.map((file) => toDataUri(file));
      } catch (err) {
        this.failAll(record, `参考素材处理失败：${(err as Error).message}`);
        return;
      }

      // 视频/音频官方只收 URL：先传到 COS，再给 7 天有效的预签名地址
      // （单条任务最长跑 30 分钟，1 小时签名太紧）。
      let videoUrls: string[] = [];
      let audioUrls: string[] = [];
      if (videoFiles.length || audioFiles.length) {
        try {
          videoUrls = await this.uploadRefs(record, videoFiles);
          audioUrls = await this.uploadRefs(record, audioFiles);
        } catch (err) {
          this.failAll(record, `参考素材上传云端失败：${(err as Error).message}`);
          return;
        }
      }

      const wire = this.wan.wireFreeBody({
        prompt: record.prompt,
        negativePrompt: record.negative_prompt,
        mode: record.mode,
        resolution: record.resolution,
        aspectRatio: record.ratio,
        duration: record.duration,
        audio: record.audio,
        promptExtend: record.prompt_extend,
        watermark: record.watermark,
        seed: record.seed,
        imageUrls,
        videoUrls,
        audioUrls,
      });

      for (const shot of record.shots) {
        if (record.cancel_requested) {
          shot.status = SHOT_CANCELED;
          shot.error = '已取消';
          this.persist(record);
          continue;
        }
        shot.status = SHOT_RUNNING;
        record.updated_at = nowText();
        this.persist(record);
        await this.runShot(record, shot, wire);
        this.persist(record);
      }
    } finally {
      this.finish(record);
    }
  }

  private async runShot(
    record: VgRecord,
    shot: VgShot,
    wire: Record<string, unknown>,
  ): Promise<void> {
    const started = Date.now();
    try {
      const submit = await this.wan.smallInflight.run(async () => {
        const result = await this.wan.submit(wire);
        shot.task_id = result.taskId;
        record.updated_at = nowText();
        this.persist(record);
        return this.wan.waitTask(result.taskId, {
          shouldStop: () => Boolean(record.cancel_requested),
        });
      });
      if (submit.status !== 'succeeded' || !submit.videoUrl) {
        throw new VideoGenError(submit.message || `云端返回 ${submit.status}`, 502);
      }
      const { seconds, cost } = this.estimate(submit.usage || {}, record.resolution);
      shot.billable_seconds = seconds;
      shot.estimated_cost = cost;
      const dest = path.join(this.taskDir(record.id), `shot-${shot.index}.mp4`);
      await this.wan.download(submit.videoUrl, dest);
      if (!fs.existsSync(dest) || fs.statSync(dest).size <= 0) {
        throw new VideoGenError('视频下载失败：文件为空。', 502);
      }
      shot.file = path.basename(dest);
      shot.status = SHOT_DONE;
      shot.error = '';
    } catch (err) {
      if (err instanceof WaitStopped) {
        shot.status = SHOT_CANCELED;
        shot.error = '已取消';
      } else if (err instanceof TaskCanceled) {
        shot.status = SHOT_CANCELED;
        shot.error = '云端任务已取消';
      } else if (err instanceof TaskExpired) {
        shot.status = SHOT_FAILED;
        shot.error = '云端任务已过期/查不到；可稍后用 task_id 到百炼控制台确认。';
      } else if (err instanceof WaitTimeout) {
        shot.status = SHOT_FAILED;
        shot.error = `等待超时（${err.message}）；云端可能已出片并计费，请到百炼控制台按 task_id 确认。`;
      } else if (err instanceof WanError || err instanceof VideoGenError) {
        shot.status = SHOT_FAILED;
        shot.error = err.message;
      } else {
        shot.status = SHOT_FAILED;
        shot.error = `生成失败：${(err as Error).message}`;
      }
    } finally {
      shot.seconds = Math.round((Date.now() - started) / 100) / 10;
    }
  }

  private estimate(usage: Record<string, unknown>, resolution: string): {
    seconds: number;
    cost: number | null;
  } {
    const seconds = usageSeconds(usage);
    const price = Number(this.config.unitPrice(resolution) || 0);
    return {
      seconds,
      cost: price ? Math.round(seconds * price * 10000) / 10000 : null,
    };
  }

  private finish(record: VgRecord, error = ''): void {
    const shots = record.shots || [];
    const done = shots.filter((shot) => shot.status === SHOT_DONE).length;
    const canceled = shots.filter((shot) => shot.status === SHOT_CANCELED).length;
    record.progress = `${done}/${shots.length}`;
    if (error && done === 0) {
      record.status = STATUS_FAILED;
      record.message = error;
    } else if (done === shots.length && done > 0) {
      record.status = STATUS_DONE;
    } else if (done > 0) {
      record.status = STATUS_PARTIAL;
    } else if (canceled === shots.length && canceled > 0) {
      record.status = STATUS_CANCELED;
    } else {
      record.status = STATUS_FAILED;
      if (!record.message) {
        const first = shots.find((shot) => shot.error)?.error || '';
        record.message = first || '生成失败';
      }
    }
    record.finished_at = nowText();
    record.updated_at = record.finished_at;
    try {
      this.persist(record);
    } catch (err) {
      this.logger.warn(`videogen 任务 ${record.id} 写盘失败：${(err as Error).message}`);
    }
  }

  private failAll(record: VgRecord, message: string): void {
    for (const shot of record.shots || []) {
      if (shot.status === SHOT_QUEUED || shot.status === SHOT_RUNNING) {
        shot.status = SHOT_FAILED;
        shot.error = message;
      }
    }
    record.message = message;
    this.finish(record);
  }

  /** 供控制器使用：把 VideoGenError 包成 HttpException（保留状态码）。 */
  static httpError(err: unknown): never {
    if (err instanceof HttpException) throw err;
    if (err instanceof VideoGenError) {
      throw new HttpException({ ok: false, error: err.message }, err.status);
    }
    throw err;
  }

  /** 测试/退出用：等待队列清空（生产里由进程退出收尾）。 */
  async drain(): Promise<void> {
    while (this.queue.length > 0 || this.running > 0) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }

  // 供控制器写临时文件（参考视频/音频走原始字节流）。
  async writeTemp(buffer: Buffer, suffix: string): Promise<string> {
    fs.mkdirSync(this.tmpDir, { recursive: true });
    const file = path.join(this.tmpDir, `${randomId()}${suffix}`);
    await fsp.writeFile(file, buffer);
    return file;
  }
}

function idList(raw: unknown, label: string): string[] {
  if (raw === null || raw === undefined || raw === '') return [];
  if (!Array.isArray(raw)) throw new VideoGenError(`${label}参数无效。`);
  const items = raw.map((item) => String(item ?? '').trim()).filter(Boolean);
  if (new Set(items).size !== items.length) {
    throw new VideoGenError(`同一段${label}不能重复提交。`);
  }
  return items;
}

function usageSeconds(usage: Record<string, unknown>): number {
  const raw = usage.duration ?? usage.output_video_duration ?? 0;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.round(value * 100) / 100;
}

function randomId(): string {
  // 与 Python uuid4().hex 同形（32 位小写 hex），便于两版数据互认。
  return randomBytes(16).toString('hex');
}
