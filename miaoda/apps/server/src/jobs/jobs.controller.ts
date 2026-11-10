/**
 * 小批量试跑的任务接口（P1）。
 *
 * 路由与 Python 版一一对应：
 * - `POST /api/jobs`（201）→ `{ok:true, job}`；`GET /api/jobs` → `{ok, jobs, ...}`
 * - `GET /api/jobs/<id>` / `POST .../cancel|retry|recheck`
 * - `GET /api/jobs/<id>/download/<tid>`、`/files.zip`、`/report.csv`、`/log`
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  BadRequestException,
  Body,
  Controller,
  ForbiddenException,
  Get,
  HttpCode,
  HttpException,
  NotFoundException,
  Param,
  Post,
  Query,
  Res,
} from '@nestjs/common';
import type { Response } from 'express';
import { AppConfigService } from '../config/app-config.service';
import { CurrentUser, type AuthedUser } from '../auth/decorators';
import { PromptsService } from '../templates/prompts.service';
import { TemplateCatalogService, TemplateError } from '../templates/template-catalog.service';
import { UploadsService } from '../uploads/uploads.service';
import { JobManagerService, type Job, type JobImageSlot } from './job-manager.service';
import { zipDirectory } from './zip';

const UPLOAD_ID_RE = /^[a-f0-9]{32}$/;

@Controller('api/jobs')
export class JobsController {
  constructor(
    private readonly config: AppConfigService,
    private readonly jobs: JobManagerService,
    private readonly uploads: UploadsService,
    private readonly catalog: TemplateCatalogService,
    private readonly prompts: PromptsService,
  ) {}

  @Post()
  @HttpCode(201)
  async create(@Body() body: Record<string, unknown>, @CurrentUser() user: AuthedUser) {
    const slots = this.resolveSlots(body, user);
    // 候选集可能还没构建过（只有 bootstrap 会主动建）；这里补一次，build() 自身有缓存。
    await this.catalog.build();
    let templates;
    try {
      templates = this.catalog.choose(
        String(body.selection || 'random'),
        Number(body.count || 1),
        String(body.template_id || ''),
        this.config.maxJobItems,
        body.tag_filters as Record<string, string[]> | undefined,
      );
    } catch (err) {
      if (err instanceof TemplateError) throw new BadRequestException(err.message);
      throw err;
    }
    const pending = templates.filter((tpl) => !tpl.url && !tpl.key).map((tpl) => tpl.id);
    if (pending.length) {
      throw new BadRequestException(
        `所选视频尚未上传到云端模板库（例如 ${pending[0]}），当前只支持标签筛选与预览；完成批量上传后即可生成。`,
      );
    }
    const promptLibrary = this.prompts.list();
    const promptId = String(body.prompt_id || promptLibrary[0]?.id || '');
    if (!promptId) {
      throw new BadRequestException('提示词库没有可用模板，请联系管理员检查 prompts/prompt-library.json。');
    }
    if (templates.length > this.config.confirmOverItems && !body.confirmed) {
      throw new ConflictError('批量数量较多，请确认后再提交。');
    }
    const promptItem = promptLibrary.find((item) => item.id === promptId);
    if (!promptItem) {
      throw new BadRequestException('请选择有效的提示词模板');
    }
    const quotaError = this.quotaCheck(user, templates.length);
    if (quotaError) throw new ForbiddenException(quotaError);

    const job = this.jobs.create({
      owner: user.username,
      slots,
      templates,
      promptId,
      // 与 Python 一致：没填 prompt 时用提示词库正文兜底（前端只传 prompt_id 也能提交）。
      prompt: String(body.prompt || promptItem.body || ''),
      resolution: String(body.resolution || '480P'),
      duration: Number(body.duration || 5),
      tags: cleanTags(body.tags),
      styleNo: String(body.style_no || ''),
    });
    return { ok: true, job: this.jobs.publicView(job, user.role === 'admin') };
  }

  @Get()
  list(@CurrentUser() user: AuthedUser, @Query('scope') scope?: string, @Query('owner') owner?: string) {
    const all = scope === 'all';
    if (all && user.role !== 'admin') throw new ForbiddenException('此操作需要管理员权限。');
    const items = this.jobs.list(user.username, all, String(owner || '').trim());
    const queued = items.filter((job) => job.status === 'queued');
    return {
      ok: true,
      jobs: items.map((job) => this.jobs.publicView(job, user.role === 'admin')),
      queued: queued.length,
      max_active: this.config.maxActiveJobs,
      max_active_per_user: this.config.maxActivePerUser,
      small_lane_items: this.config.smallLaneItems,
      max_active_small_jobs: this.config.maxActiveSmallJobs,
    };
  }

  @Get(':id')
  detail(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    const job = this.authorized(id, user);
    return { ok: true, job: this.jobs.publicView(job, user.role === 'admin') };
  }

  @Post(':id/cancel')
  @HttpCode(200)
  cancel(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    const job = this.jobs.get(id);
    if (!job) throw new NotFoundException('找不到该任务。');
    if (user.role !== 'admin' && job.owner !== user.username) throw new ForbiddenException('无权操作该任务。');
    if (!this.jobs.cancel(id)) throw new ConflictError('该任务目前不能取消。');
    return { ok: true };
  }

  @Post(':id/retry')
  @HttpCode(200)
  retry(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    try {
      const job = this.jobs.retryFailed(id, user.username, user.role === 'admin');
      return { ok: true, job: this.jobs.publicView(job, user.role === 'admin') };
    } catch (err) {
      throw jobError(err);
    }
  }

  /** 复查超时/下载失败条目：只查云端、不重新提交（重提会二次计费）。 */
  @Post(':id/recheck')
  @HttpCode(202)
  recheck(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    const job = this.jobs.get(id);
    if (!job) throw new NotFoundException('找不到该任务。');
    if (user.role !== 'admin' && job.owner !== user.username) throw new ForbiddenException('无权操作该任务。');
    try {
      const snapshot = this.jobs.recheckTimeouts(id);
      return { ok: true, job: this.jobs.publicView(snapshot, user.role === 'admin') };
    } catch (err) {
      throw jobError(err);
    }
  }

  @Get(':id/log')
  log(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    const job = this.authorized(id, user);
    const file = this.jobs.logPath(job.id);
    const text = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
    return { ok: true, log: text };
  }

  @Get(':id/download/:templateId')
  download(
    @Param('id') id: string,
    @Param('templateId') templateId: string,
    @CurrentUser() user: AuthedUser,
    @Res() res: Response,
  ): void {
    const job = this.authorized(id, user);
    if (!job.template_ids.includes(templateId)) throw new NotFoundException('找不到该视频。');
    const record = job.records.find((rec) => rec.id === templateId);
    const file = path.join(job.out_dir, `${templateId}.mp4`);
    if (!record || record.status !== 'success' || !job.out_dir || !fs.existsSync(file)) {
      throw new NotFoundException('视频还没有生成完成。');
    }
    const inline = String(res.req.headers['x-playback'] || '') === '1';
    res.setHeader('Content-Type', 'video/mp4');
    res.setHeader('Accept-Ranges', 'bytes');
    res.setHeader(
      'Content-Disposition',
      `${inline ? 'inline' : 'attachment'}; filename*=UTF-8''${encodeURIComponent(`${job.id}-${templateId}.mp4`)}`,
    );
    res.sendFile(path.resolve(file));
  }

  @Get(':id/files.zip')
  zip(@Param('id') id: string, @CurrentUser() user: AuthedUser, @Res() res: Response): void {
    const job = this.authorized(id, user);
    if (!job.out_dir || !fs.existsSync(job.out_dir)) throw new NotFoundException('暂时没有可下载的视频。');
    const files = fs.readdirSync(job.out_dir).filter((name) => name.toLowerCase().endsWith('.mp4'));
    if (!files.length) throw new NotFoundException('暂时没有可下载的视频。');
    const buffer = zipDirectory(job.out_dir);
    res.setHeader('Content-Type', 'application/zip');
    res.setHeader('Content-Disposition', `attachment; filename*=UTF-8''${encodeURIComponent(`${job.id}.zip`)}`);
    res.end(buffer);
  }

  @Get(':id/report.csv')
  report(@Param('id') id: string, @CurrentUser() user: AuthedUser, @Res() res: Response): void {
    const job = this.authorized(id, user);
    const file = job.out_dir ? path.join(job.out_dir, 'report.csv') : '';
    if (!file || !fs.existsSync(file)) throw new NotFoundException('报告暂不可下载。');
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename*=UTF-8''${encodeURIComponent(`${job.id}-report.csv`)}`);
    res.sendFile(path.resolve(file));
  }

  // ---------------------------------------------------------------- 辅助

  private authorized(jobId: string, user: AuthedUser): Job {
    const job = this.jobs.get(jobId);
    if (!job) throw new NotFoundException('找不到该任务。');
    if (user.role !== 'admin' && job.owner !== user.username) throw new ForbiddenException('无权操作该任务。');
    return job;
  }

  private resolveSlots(body: Record<string, unknown>, user: AuthedUser): JobImageSlot[] {
    const raw = Array.isArray(body.upload_ids)
      ? body.upload_ids
      : body.upload_id
        ? [body.upload_id]
        : [];
    const ids = raw.map((item) => String(item || '').trim()).filter(Boolean);
    if (!ids.length) throw new BadRequestException('请先上传服装图片。');
    if (ids.length > 10) {
      throw new BadRequestException(
        `一次最多 10 张图片（1 张定妆图 + 9 张细节图），当前 ${ids.length} 张。`,
      );
    }
    if (new Set(ids).size !== ids.length) throw new BadRequestException('同一张图片不能重复提交。');
    const slots: JobImageSlot[] = [];
    ids.forEach((uploadId, index) => {
      if (!UPLOAD_ID_RE.test(uploadId)) throw new NotFoundException(`第 ${index + 1} 张图片已失效，请重新上传。`);
      const meta = this.uploads.resolve(uploadId, user.username);
      if (!meta) throw new NotFoundException(`第 ${index + 1} 张图片已失效，请重新上传。`);
      slots.push({ upload_id: uploadId, name: String(meta.meta.name || ''), path: meta.file });
    });
    return slots;
  }

  private quotaCheck(user: AuthedUser, count: number): string {
    const quota = Number(user.record.quota || 0);
    if (!Number.isFinite(quota) || quota <= 0) return '';
    const used = this.jobs.list(user.username, false).reduce(
      (total, job) => total + (job.status === 'canceled' ? 0 : job.total),
      0,
    );
    if (used + count > quota) {
      return (
        `生成数量已达上限：该账户已用 ${used} 条 / 上限 ${quota} 条，` +
        `本次 ${count} 条会超出额度；请联系管理员调整。`
      );
    }
    return '';
  }
}

class ConflictError extends HttpException {
  constructor(message: string) {
    super({ ok: false, error: message }, 409);
  }
}

/** JobManager 抛的是普通 Error（Python 侧对应 ValueError → 409）：按文案映射成合适的 HTTP 码。 */
function jobError(err: unknown): HttpException {
  const message = (err as Error)?.message || '操作失败';
  if (message.includes('找不到该任务')) return new NotFoundException(message);
  if (message.includes('无权操作')) return new ForbiddenException(message);
  return new ConflictError(message);
}

const TAG_SLOTS = ['top', 'lining', 'pants', 'shoes', 'scene', 'note'] as const;
const TAG_CODE_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;

/** 6 栏位标签清洗（上衣必填由 JobManager 校验）。 */
function cleanTags(raw: unknown): Record<string, string> {
  if (!raw || typeof raw !== 'object') return {};
  const out: Record<string, string> = {};
  for (const slot of TAG_SLOTS) {
    const value = String((raw as Record<string, unknown>)[slot] ?? '').trim();
    if (!value) continue;
    if (slot === 'note') {
      out.note = value.slice(0, 100);
      continue;
    }
    if (!TAG_CODE_RE.test(value)) {
      throw new BadRequestException(`「${slot}」只能填字母、数字、下划线或短横线（最多 40 字）`);
    }
    out[slot] = value;
  }
  return out;
}

export { TemplateError };
