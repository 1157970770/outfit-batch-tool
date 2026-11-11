/**
 * 自由视频接口（对应 `app/web/server.py` 的 `_videogen_*`）。
 *
 * 路径与状态码严格对齐 Python 版：
 *  - `GET    /api/videogen/tasks`            200 `{ok,items,configured,reason}`
 *  - `POST   /api/videogen/tasks`            201 `{ok,task}`
 *  - `GET    /api/videogen/tasks/:id`        200 `{ok,task}`
 *  - `DELETE /api/videogen/tasks/:id`        200 `{ok}`
 *  - `POST   /api/videogen/tasks/:id/cancel` 200 `{ok,task}`
 *  - `POST   /api/videogen/upload`           201 `{ok,upload}`（原始字节流）
 *  - `GET    /api/videogen/tasks/:id/videos/:index[?download=1]`
 *  - `GET    /api/videogen/tasks/:id/refs/:kind/:index[?download=1]`
 */
import {
  BadRequestException,
  Controller,
  Delete,
  Get,
  HttpCode,
  HttpException,
  NotFoundException,
  Param,
  Post,
  Query,
  Req,
  Res,
} from '@nestjs/common';
import type { Request, Response } from 'express';
import * as path from 'node:path';

import { AuthedUser, CurrentUser } from '../auth/decorators';
import { UploadsService, UPLOAD_ID_RE } from '../uploads/uploads.service';
import { VideogenManagerService } from './videogen-manager.service';
import { VideoGenError } from './videogen-models';

const DOWNLOAD_TRUTHY = new Set(['1', 'true', 'yes', 'on']);

@Controller('api/videogen')
export class VideogenController {
  constructor(
    private readonly videogen: VideogenManagerService,
    private readonly uploads: UploadsService,
  ) {}

  @Get('tasks')
  list(@CurrentUser() user: AuthedUser, @Query('limit') limit?: string) {
    const parsed = Number.parseInt(String(limit ?? ''), 10);
    const capped = Number.isFinite(parsed) ? parsed : 40;
    return {
      ok: true,
      items: this.videogen.listTasks({ username: user.username, role: user.role }, capped),
      configured: this.videogen.configured,
      reason: this.videogen.unconfiguredReason(),
    };
  }

  @Post('tasks')
  @HttpCode(201)
  create(@CurrentUser() user: AuthedUser, @Req() req: Request) {
    const body = (req.body || {}) as Record<string, unknown>;
    const actor = { username: user.username, role: user.role };
    try {
      const images = this.resolveImages(body, user);
      const task = this.videogen.createTask(actor, body, images);
      return { ok: true, task };
    } catch (err) {
      VideogenManagerService.httpError(err);
    }
  }

  @Post('tasks/:id/cancel')
  @HttpCode(200)
  cancel(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    try {
      return {
        ok: true,
        task: this.videogen.cancelTask(id, { username: user.username, role: user.role }),
      };
    } catch (err) {
      VideogenManagerService.httpError(err);
    }
  }

  @Delete('tasks/:id')
  @HttpCode(200)
  remove(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    try {
      this.videogen.deleteTask(id, { username: user.username, role: user.role });
      return { ok: true };
    } catch (err) {
      VideogenManagerService.httpError(err);
    }
  }

  @Get('tasks/:id/videos/:index')
  video(
    @Param('id') id: string,
    @Param('index') rawIndex: string,
    @Query('download') download: string | undefined,
    @CurrentUser() user: AuthedUser,
    @Res() res: Response,
  ): void {
    if (!/^[a-f0-9]{32}$/.test(String(id || ''))) {
      throw new NotFoundException({ ok: false, error: '找不到这条视频。' });
    }
    const index = Number.parseInt(String(rawIndex ?? ''), 10);
    if (!Number.isFinite(index)) {
      throw new NotFoundException({ ok: false, error: '找不到这条视频。' });
    }
    let file: { path: string; name: string };
    try {
      file = this.videogen.videoFile(id, index, { username: user.username, role: user.role });
    } catch (err) {
      VideogenManagerService.httpError(err);
    }
    const attachment = DOWNLOAD_TRUTHY.has(String(download || '').toLowerCase());
    res.setHeader('Content-Type', 'video/mp4');
    res.setHeader('Cache-Control', 'private, max-age=300');
    sendFile(res, file.path, file.name, attachment);
  }

  @Get('tasks/:id/refs/:kind/:index')
  reference(
    @Param('id') id: string,
    @Param('kind') kind: string,
    @Param('index') rawIndex: string,
    @Query('download') download: string | undefined,
    @CurrentUser() user: AuthedUser,
    @Res() res: Response,
  ): void {
    if (!/^[a-f0-9]{32}$/.test(String(id || ''))) {
      throw new NotFoundException({ ok: false, error: '找不到这个素材。' });
    }
    const index = Number.parseInt(String(rawIndex ?? ''), 10);
    if (!Number.isFinite(index)) {
      throw new NotFoundException({ ok: false, error: '找不到这个素材。' });
    }
    let file: { path: string; name: string };
    try {
      file = this.videogen.referenceFile(id, kind, index, {
        username: user.username,
        role: user.role,
      });
    } catch (err) {
      VideogenManagerService.httpError(err);
    }
    const attachment = DOWNLOAD_TRUTHY.has(String(download || '').toLowerCase());
    res.setHeader('Content-Type', mimeOf(file.path));
    res.setHeader('Cache-Control', 'private, max-age=300');
    sendFile(res, file.path, file.name, attachment);
  }

  @Get('tasks/:id')
  detail(@Param('id') id: string, @CurrentUser() user: AuthedUser) {
    try {
      return {
        ok: true,
        task: this.videogen.getRecord(id, { username: user.username, role: user.role }),
      };
    } catch (err) {
      VideogenManagerService.httpError(err);
    }
  }

  /**
   * 参考视频 / 音频上传：原始字节流（`main.ts` 里对这个路径挂了 express.raw）。
   * 头：`X-Kind: video|audio`、`X-File-Name: <encodeURIComponent 过的文件名>`。
   */
  @Post('upload')
  @HttpCode(201)
  async upload(@CurrentUser() user: AuthedUser, @Req() req: Request) {
    const body = req.body as unknown;
    if (!Buffer.isBuffer(body) || body.length === 0) {
      throw new BadRequestException({ ok: false, error: '请选择要上传的素材。' });
    }
    const kind = String(req.headers['x-kind'] || '').trim().toLowerCase();
    if (kind !== 'video' && kind !== 'audio') {
      throw new BadRequestException({ ok: false, error: '素材类型无效。' });
    }
    const limitMb = kind === 'video' ? 100 : 15;
    if (body.length > limitMb * 1024 * 1024) {
      throw new HttpException(
        {
          ok: false,
          error: `单个${kind === 'video' ? '视频' : '音频'}不能超过 ${limitMb}MB。`,
        },
        413,
      );
    }
    const rawName = decodeHeader(req.headers['x-file-name']);
    const filename =
      path.basename(rawName) || (kind === 'video' ? 'reference.mp4' : 'reference.mp3');
    const suffix = path.extname(filename).toLowerCase();
    const allowed = kind === 'video' ? ['.mp4', '.mov', '.m4v'] : ['.wav', '.mp3'];
    if (!allowed.includes(suffix)) {
      throw new BadRequestException({
        ok: false,
        error: '视频只收 mp4/mov/m4v，音频只收 wav/mp3。',
      });
    }
    let temp = '';
    try {
      temp = await this.videogen.writeTemp(body, suffix);
      const upload = this.videogen.addUpload(
        { username: user.username, role: user.role },
        temp,
        filename,
        kind,
      );
      return { ok: true, upload };
    } catch (err) {
      VideogenManagerService.httpError(err);
    } finally {
      if (temp) {
        try {
          const fs = await import('node:fs');
          fs.rmSync(temp, { force: true });
        } catch {
          /* 临时文件删不掉不影响主流程 */
        }
      }
    }
  }

  /** 参考图槽位：`upload_ids`（数组）优先，回退单个 `upload_id`；允许为空。 */
  private resolveImages(
    body: Record<string, unknown>,
    user: AuthedUser,
  ): Array<{ path: string; name: string }> {
    const raw = Array.isArray(body.upload_ids)
      ? body.upload_ids
      : body.upload_id
        ? [body.upload_id]
        : [];
    const ids = raw.map((item) => String(item || '').trim()).filter(Boolean);
    if (ids.length > 10) {
      throw new BadRequestException(
        `一次最多 10 张图片（1 张定妆图 + 9 张细节图），当前 ${ids.length} 张。`,
      );
    }
    if (new Set(ids).size !== ids.length) {
      throw new BadRequestException('同一张图片不能重复提交。');
    }
    return ids.map((uploadId, index) => {
      if (!UPLOAD_ID_RE.test(uploadId)) {
        throw new NotFoundException(`第 ${index + 1} 张图片已失效，请重新上传。`);
      }
      const found = this.uploads.resolve(uploadId, user.username);
      if (!found) throw new NotFoundException(`第 ${index + 1} 张图片已失效，请重新上传。`);
      return { path: found.file, name: String(found.meta.name || '') };
    });
  }
}

function sendFile(res: Response, file: string, name: string, attachment: boolean): void {
  const disposition = attachment ? 'attachment' : 'inline';
  res.setHeader(
    'Content-Disposition',
    `${disposition}; filename*=UTF-8''${encodeURIComponent(name)}`,
  );
  res.sendFile(file);
}

function mimeOf(file: string): string {
  const ext = path.extname(file).toLowerCase();
  if (ext === '.mp4' || ext === '.m4v') return 'video/mp4';
  if (ext === '.mov') return 'video/quicktime';
  if (ext === '.mp3') return 'audio/mpeg';
  if (ext === '.wav') return 'audio/wav';
  if (ext === '.png') return 'image/png';
  if (ext === '.webp') return 'image/webp';
  if (ext === '.gif') return 'image/gif';
  return 'application/octet-stream';
}

function decodeHeader(value: string | string[] | undefined): string {
  const raw = Array.isArray(value) ? value[0] : value;
  if (!raw) return '';
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export { VideoGenError };
